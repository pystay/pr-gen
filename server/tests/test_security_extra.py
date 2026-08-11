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
from tests.conftest import SECRET, make_client

PROD_WEBHOOK_SECRET = "prod-secret-please-change"
PROD_USAGE_KEY = "prod-usage-key"


class _ProdMixin:
    """生产模式环境：非本地（配置了外部服务）+ 显式 API key。"""

    def _set_prod_env(self) -> dict:
        return {
            "USAGE_API_KEY": PROD_USAGE_KEY,
            "TELEGRAM_BOT_TOKEN": "tg_x_prod",  # 使 is_local_mode=False
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

        return admin_token(make_client(self.tmp, **self._set_prod_env())
                           .app.state.settings.data_encryption_key)

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

    def test_cron_requires_api_key(self):
        r = self.client.post("/api/github/cron/reset-usage")
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            "/api/github/cron/reset-usage", headers={"X-API-Key": PROD_USAGE_KEY}
        )
        self.assertEqual(r.status_code, 200)

    def test_prod_refuses_missing_usage_key(self):
        """生产模式（非本地）下缺少 USAGE_API_KEY 必须拒绝启动（fail-fast）。"""
        import os

        from fastapi.testclient import TestClient

        from app.main import app as app_obj

        os.environ.update({
            "USAGE_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "tg_x_prod",
        })
        tmp2 = Path(tempfile.mkdtemp(prefix="prgen-failfast-"))
        os.environ["DATA_DIR"] = str(tmp2 / "data")
        try:
            with TestClient(app_obj):
                self.fail("生产模式缺少 USAGE_API_KEY 应拒绝启动")
        except RuntimeError as exc:
            self.assertIn("USAGE_API_KEY", str(exc))
        finally:
            import shutil

            shutil.rmtree(tmp2, ignore_errors=True)
            # 恢复生产环境
            self.client = make_client(self.tmp, **self._set_prod_env())


