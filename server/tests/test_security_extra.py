"""安全加固测试：生产模式鉴权、并发幂等、XSS/注入防护、舍入不变量。"""

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database
from app.quoting import compute_quote, QuoteRequest
from tests.conftest import SECRET, make_client, purchase_payload, signed_payload

PROD_WEBHOOK_SECRET = "prod-secret-please-change"
PROD_USAGE_KEY = "prod-usage-key"


class _ProdMixin:
    """生产模式环境：非本地（配置了外部服务）+ 显式 API key。"""

    def _set_prod_env(self) -> dict:
        return {
            "GITHUB_WEBHOOK_SECRET": PROD_WEBHOOK_SECRET,
            "USAGE_API_KEY": PROD_USAGE_KEY,
            "PAY_SECRET_KEY": "prod-pay-secret",
            "RESEND_API_KEY": "re_x_prod",  # 使 is_local_mode=False
        }


class TestProdAuth(_ProdMixin, unittest.TestCase):
    """生产模式下管理端点与内部 API 必须鉴权。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-prod-"))
        self.client: TestClient = make_client(self.tmp, **self._set_prod_env())

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prod_token(self) -> str:
        from app.security import admin_token

        return admin_token(PROD_WEBHOOK_SECRET)

    def test_stats_requires_login(self):
        resp = self.client.get("/api/admin/stats")
        self.assertEqual(resp.status_code, 403)

    def test_admin_login_flow(self):
        # 错误 token
        r = self.client.post("/api/admin/login", json={"token": "wrong"})
        self.assertEqual(r.status_code, 401)
        # 正确 token → cookie
        r = self.client.post("/api/admin/login", json={"token": self._prod_token()})
        self.assertEqual(r.status_code, 200)
        self.assertIn("set-cookie", r.headers)
        # 带 cookie 访问 stats
        cookie = r.headers["set-cookie"].split(";")[0]
        resp = self.client.get("/api/admin/stats", headers={"Cookie": cookie})
        self.assertEqual(resp.status_code, 200)
        # admin 页面不内嵌 token
        page = self.client.get("/admin").text
        self.assertNotIn(self._prod_token(), page)

    def test_usage_requires_api_key(self):
        r = self.client.post("/api/usage", json={"account_id": "u", "cost": 99})
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            "/api/usage", json={"account_id": "u", "cost": 0.1},
            headers={"X-API-Key": PROD_USAGE_KEY},
        )
        self.assertEqual(r.status_code, 200)

    def test_cron_downgrade_requires_api_key(self):
        r = self.client.post("/api/github/cron/downgrade")
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            "/api/github/cron/downgrade", headers={"X-API-Key": PROD_USAGE_KEY}
        )
        self.assertEqual(r.status_code, 200)

    def test_prod_refuses_default_secrets(self):
        """生产模式（非本地）下默认密钥必须拒绝启动（fail-fast）。"""
        import os
        from contextlib import contextmanager

        from fastapi.testclient import TestClient

        from app.main import app as app_obj

        @contextmanager
        def _client():
            with TestClient(app_obj) as c:
                yield c

        # 非本地 + 默认 GITHUB_WEBHOOK_SECRET → 启动必须失败
        os.environ.update({
            "GITHUB_WEBHOOK_SECRET": "dev-webhook-secret",
            "USAGE_API_KEY": PROD_USAGE_KEY,
            "PAY_SECRET_KEY": "prod-pay-secret",
            "RESEND_API_KEY": "re_x_prod",
        })
        tmp2 = Path(tempfile.mkdtemp(prefix="prgen-failfast-"))
        os.environ["DATA_DIR"] = str(tmp2 / "data")
        try:
            with TestClient(app_obj):
                self.fail("生产模式默认密钥应拒绝启动")
        except RuntimeError as exc:
            self.assertIn("GITHUB_WEBHOOK_SECRET", str(exc))
        finally:
            import shutil

            shutil.rmtree(tmp2, ignore_errors=True)
            # 恢复生产环境
            self.client = make_client(self.tmp, **self._set_prod_env())


class TestConcurrentIdempotency(unittest.TestCase):
    """并发重投递同一事件：只允许一个请求创建订单（GitHub 会重试）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-conc-"))
        self.client: TestClient = make_client(self.tmp)
        self.db = Database(self.tmp / "data" / "prgen.db")

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_duplicate_webhooks(self):
        payload = purchase_payload()
        sp = signed_payload(payload)

        def post(_):
            return self.client.post(
                "/api/github/webhook", content=sp["body"],
                headers={"X-Hub-Signature-256": sp["signature"],
                         "X-GitHub-Event": "marketplace_purchase"},
            ).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(post, range(4)))
        self.assertTrue(all(c == 200 for c in codes))
        # 无论并发顺序如何，订单只有一条
        self.assertEqual(len(self.db.list_orders()), 1)


class TestInjectionProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-inj-"))
        self.client: TestClient = make_client(self.tmp)

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_email_path_injection_rejected(self):
        bad = {
            "company": "测试",
            "contact_email": "cto@x.com/../../evil",
            "dev_count": 15,
            "environment": "aws",
        }
        resp = self.client.post("/api/enterprise/quote", json=bad)
        self.assertEqual(resp.status_code, 422)

    def test_html_injection_escaped_in_email(self):
        evil = {"company": "<script>alert(1)</script>", "contact_email": "a@b.com",
                "dev_count": 15, "environment": "aws"}
        resp = self.client.post("/api/enterprise/quote", json=evil)
        self.assertEqual(resp.status_code, 200)
        meta = json.loads(Path(resp.json()["mail"]["meta_file"]).read_text(
            encoding="utf-8"))
        self.assertIn("&lt;script&gt;", meta["html"])
        self.assertNotIn("<script>", meta["html"])
        # PDF 生成不应因特殊字符崩溃（Paragraph 转义）
        self.assertTrue(Path(resp.json()["pdf"]).exists())

    def test_rounding_invariant(self):
        """分解项展示值之和必须等于 total（金额一致性）。"""
        from app.config import load_settings

        settings = load_settings()
        for devs in (1, 5, 10, 11, 15, 27, 100):
            for env in ("aws", "private_idc", "hybrid"):
                req = QuoteRequest(company="TT", contact_email="a@b.com",
                                   dev_count=devs, environment=env,
                                   needs_custom=True)
                q = compute_quote(req, settings)
                parts = q.base_price + q.deployment_fee + q.custom_fee
                self.assertEqual(parts, q.total,
                                 f"devs={devs} env={env} 分解项之和不等于 total")


if __name__ == "__main__":
    unittest.main()
