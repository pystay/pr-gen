"""支付宝 AI 网页应用收款通道测试。

覆盖：
- 未配置凭证时明确报错（不 fallback 占位密钥）
- RSA2 通知验签（用 SDK 签名/验签双向验证）
- 通知处理：验签 → 业务校验 → 幂等 → 激活订阅
- 金额规范化 / 付款状态判定
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.config import load_settings
from app.db import Database
from app.alipay_pay import (
    _is_paid_notification, _normalize_amount, verify_notify_signature,
)
from tests.conftest import make_client


def _make_rsa_keys() -> tuple[str, str]:
    """生成 (私钥 PEM, 公钥 PEM)。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,  # PKCS#1
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private, public


PRIVATE, PUBLIC = _make_rsa_keys()
APP_ID = "2026000000000001"
SELLER_ID = "2088000000000001"


def _sdk_sign(params: dict, private_key: str = PRIVATE) -> str:
    """用官方 SDK 生成 RSA2 签名（与支付宝网关一致）。"""
    from alipay.aop.api.util import SignatureUtils

    to_sign = {k: v for k, v in params.items() if k not in ("sign", "sign_type")}
    content = SignatureUtils.get_sign_content(to_sign)
    return SignatureUtils.sign_with_rsa2(private_key, content, "utf-8")


def _make_env() -> dict:
    return {
        "ALIPAY_APP_ID": APP_ID,
        "ALIPAY_APP_PRIVATE_KEY": PRIVATE,
        "ALIPAY_PUBLIC_KEY": PUBLIC,
        "ALIPAY_SELLER_ID": SELLER_ID,
        "ALIPAY_SANDBOX": "true",
    }


class TestAlipaySignature(unittest.TestCase):
    def test_verify_with_sdk_signed_params(self):
        params = {"app_id": APP_ID, "out_trade_no": "T1", "total_amount": "9.90",
                  "trade_status": "TRADE_SUCCESS", "notify_id": "N1"}
        params["sign"] = _sdk_sign(params)
        settings = load_settings(env_overrides=_make_env())
        self.assertTrue(verify_notify_signature(settings, params))

    def test_verify_rejects_tampered(self):
        params = {"app_id": APP_ID, "out_trade_no": "T1", "total_amount": "9.90",
                  "trade_status": "TRADE_SUCCESS", "notify_id": "N1"}
        params["sign"] = _sdk_sign(params)
        params["total_amount"] = "999.00"  # 篡改金额（未重新签名）
        settings = load_settings(env_overrides=_make_env())
        self.assertFalse(verify_notify_signature(settings, params))

    def test_verify_missing_sign(self):
        settings = load_settings(env_overrides=_make_env())
        self.assertFalse(verify_notify_signature(settings, {"app_id": APP_ID}))


class TestAlipayHelpers(unittest.TestCase):
    def test_normalize_amount(self):
        self.assertEqual(_normalize_amount("9.9"), "9.90")
        self.assertEqual(_normalize_amount("9"), "9.00")
        self.assertEqual(_normalize_amount("9.90"), "9.90")
        self.assertIsNone(_normalize_amount("abc"))
        self.assertIsNone(_normalize_amount(None))

    def test_paid_status(self):
        self.assertTrue(_is_paid_notification({"trade_status": "TRADE_SUCCESS"}))
        self.assertTrue(_is_paid_notification({"trade_status": "TRADE_FINISHED"}))
        self.assertFalse(_is_paid_notification({"trade_status": "WAIT_BUYER_PAY"}))
        # 退款事件不算付款成功
        self.assertFalse(_is_paid_notification(
            {"trade_status": "TRADE_SUCCESS", "refund_fee": "1.00"}))
        self.assertFalse(_is_paid_notification(
            {"trade_status": "TRADE_SUCCESS", "out_biz_no": "X"}))


class TestAlipayNotifyFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-alipay-"))
        self.client = make_client(self.tmp, **_make_env())
        self.db = Database(self.tmp / "data" / "prgen.db")

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_and_order(self) -> tuple[int, dict]:
        uid = self.client.post("/api/auth/register",
                               json={"email": "a@b.com"}).json()["user_id"]
        # mock page_execute 避免真实网关
        with mock.patch("app.alipay_pay.build_alipay_client") as m:
            fake = mock.Mock()
            fake.page_execute.return_value = "<form>mock alipay form</form>"
            m.return_value = fake
            resp = self.client.post("/api/payment/alipay/create",
                                    json={"user_id": uid, "tier": "pro",
                                          "cycle": "monthly"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["mode"], "alipay_page")
        self.assertIn("<form>", data["form_html"])
        return uid, data

    def _notify(self, order_no: str, amount: str = "9.90",
                status: str = "TRADE_SUCCESS", notify_id: str = "N1",
                app_id: str = APP_ID, seller_id: str = SELLER_ID):
        params = {
            "app_id": app_id, "out_trade_no": order_no,
            "total_amount": amount, "trade_status": status,
            "notify_id": notify_id, "seller_id": seller_id,
            "trade_no": "2026" + order_no[-8:],
        }
        params["sign"] = _sdk_sign(params)
        return self.client.post("/api/payment/alipay/notify", data=params)

    def test_create_requires_config(self):
        # 未配置凭证 → 明确报错（先清掉可能残留的 ALIPAY 环境变量）
        import os

        for k in ("ALIPAY_APP_ID", "ALIPAY_APP_PRIVATE_KEY",
                  "ALIPAY_PUBLIC_KEY", "ALIPAY_SELLER_ID"):
            os.environ.pop(k, None)
        tmp2 = Path(tempfile.mkdtemp(prefix="prgen-alipay-noconfig-"))
        client2 = make_client(tmp2)  # 无 ALIPAY_* 环境
        uid = client2.post("/api/auth/register",
                           json={"email": "n@b.com"}).json()["user_id"]
        resp = client2.post("/api/payment/alipay/create",
                            json={"user_id": uid, "tier": "pro"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("未配置", resp.json()["error"])
        client2.close()

    def test_notify_activates_subscription(self):
        uid, order = self._register_and_order()
        resp = self._notify(order["order_no"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "success")
        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["plan"], "pro")
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["payment_channel"], "alipay")
        # 订单标记成功
        self.assertEqual(self.db.get_payment(order["order_no"])["status"], "success")

    def test_notify_idempotent(self):
        uid, order = self._register_and_order()
        self._notify(order["order_no"], notify_id="N-DUP")
        # 重复 notify_id → success 且不重复处理
        resp = self._notify(order["order_no"], notify_id="N-DUP")
        self.assertEqual(resp.text, "success")
        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["plan"], "pro")

    def test_notify_bad_signature_fails(self):
        _, order = self._register_and_order()
        params = {"app_id": APP_ID, "out_trade_no": order["order_no"],
                  "total_amount": "9.90", "trade_status": "TRADE_SUCCESS",
                  "notify_id": "N-BAD", "sign": "forged"}
        resp = self.client.post("/api/payment/alipay/notify", data=params)
        self.assertEqual(resp.text, "fail")
        self.assertEqual(self.db.get_payment(order["order_no"])["status"], "pending")

    def test_notify_amount_mismatch_fails(self):
        _, order = self._register_and_order()
        resp = self._notify(order["order_no"], amount="0.01")
        self.assertEqual(resp.text, "fail")

    def test_notify_wrong_app_id_fails(self):
        _, order = self._register_and_order()
        resp = self._notify(order["order_no"], app_id="9999999999999999")
        self.assertEqual(resp.text, "fail")

    def test_notify_non_paid_status_no_activation(self):
        uid, order = self._register_and_order()
        resp = self._notify(order["order_no"], status="WAIT_BUYER_PAY")
        self.assertEqual(resp.text, "success")  # 合法通知，但未付款
        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["plan"], "free")  # 未激活

    def test_return_page_does_not_trust_params(self):
        resp = self.client.get("/api/payment/alipay/return?out_trade_no=T1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("以站内订单状态为准", resp.text)


if __name__ == "__main__":
    unittest.main()
