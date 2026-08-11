"""验收 2：企业报价 —— 15 人 + 私有 IDC → 30s 内 PDF 报价单 + 邮件，金额 198.75。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.config import load_settings
from app.db import Database
from app.quoting import compute_quote, QuoteRequest
from tests.conftest import make_client

QUOTE_FORM = {
    "company": "星辰科技",
    "contact_email": "cto@xingchen.example.com",
    "dev_count": 15,
    "environment": "private_idc",
    "needs_custom": False,
    "special_notes": "需要等保三级合规支持",
}


class TestQuotingAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-test-"))
        self.client = make_client(self.tmp)
        self.settings = load_settings()
        self.db = Database(self.tmp / "data" / "prgen.db")

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 价格模型（验收公式） ----------

    def test_quote_formula(self):
        req = QuoteRequest(**QUOTE_FORM)
        quote = compute_quote(req, self.settings)
        # 99 + (15-10)*12 = 159；私有 IDC 附加 159*0.25 = 39.75；合计 198.75
        self.assertEqual(quote.base_price, 159.0)
        self.assertEqual(quote.deployment_fee, 39.75)
        self.assertEqual(quote.custom_fee, 0.0)
        self.assertEqual(quote.total, 198.75)

    def test_quote_custom_fee(self):
        req = QuoteRequest(**{**QUOTE_FORM, "needs_custom": True, "environment": "aws"})
        quote = compute_quote(req, self.settings)
        # 159 + 159*0.40 = 222.60（AWS 无部署附加费）
        self.assertEqual(quote.total, 222.60)

    def test_quote_custom_and_idc(self):
        req = QuoteRequest(**{**QUOTE_FORM, "needs_custom": True})
        quote = compute_quote(req, self.settings)
        # 159 + 39.75 + 63.60 = 262.35（私有 IDC + 定制）
        self.assertEqual(quote.total, 262.35)

    def test_quote_below_min_devs(self):
        req = QuoteRequest(**{**QUOTE_FORM, "dev_count": 5, "environment": "aws"})
        quote = compute_quote(req, self.settings)
        self.assertEqual(quote.base_price, 99.0)  # max(0, 5-10)=0
        self.assertEqual(quote.total, 99.0)  # 无附加

    def test_form_validation(self):
        bad = {**QUOTE_FORM, "contact_email": "not-an-email"}
        resp = self.client.post("/api/enterprise/quote", json=bad)
        self.assertEqual(resp.status_code, 422)
        bad2 = {**QUOTE_FORM, "dev_count": 0}
        self.assertEqual(
            self.client.post("/api/enterprise/quote", json=bad2).status_code, 422
        )

    # ---------- 验收 2：端到端（PDF + 邮件 + leads + 30s） ----------

    def test_end_to_end_quote(self):
        start = time.monotonic()
        resp = self.client.post("/api/enterprise/quote", json=QUOTE_FORM)
        elapsed = time.monotonic() - start
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])

        # 金额正确
        self.assertEqual(data["quote"]["total"], 198.75)
        self.assertEqual(data["quote"]["base_price"], 159.0)
        self.assertEqual(data["quote"]["deployment_fee"], 39.75)

        # PDF 已生成（结构完整：%PDF 头 + %%EOF 尾 + 非空）
        pdf = Path(data["pdf"])
        self.assertTrue(pdf.exists())
        self.assertGreater(pdf.stat().st_size, 1000)
        content = pdf.read_bytes()
        self.assertIn(b"%PDF", content[:8])
        self.assertIn(b"%%EOF", content[-64:])

        # 邮件已发送（本地落盘模式）；金额出现在邮件正文
        mail = data["mail"]
        self.assertEqual(mail["channel"], "file")
        meta = json.loads(Path(mail["meta_file"]).read_text(encoding="utf-8"))
        self.assertEqual(meta["to"], QUOTE_FORM["contact_email"])
        self.assertIn("报价", meta["subject"])
        self.assertIn("198.75", meta["html"])  # 报价金额正确
        self.assertTrue(Path(mail["attachment_copy"]).exists())

        # 30 秒内完成（本地毫秒级）
        self.assertLess(elapsed, 30, f"报价生成耗时 {elapsed:.2f}s，超过 30s 上限")

        # leads 已入库且敏感字段加密
        leads = self.db.list_leads()
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["dev_count"], 15)
        self.assertEqual(lead["environment"], "private_idc")
        self.assertEqual(lead["quote_amount"], 198.75)
        self.assertEqual(lead["status"], "已报价")
        # 敏感字段是密文，不是明文
        self.assertNotIn(QUOTE_FORM["company"], lead["company_enc"])
        self.assertNotIn(QUOTE_FORM["contact_email"], lead["contact_email_enc"])

    def test_human_intervention_threshold(self):
        big = {**QUOTE_FORM, "dev_count": 60}
        resp = self.client.post("/api/enterprise/quote", json=big)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["intervention"]["channel"], "log")
        notes = self.db.list_notifications()
        self.assertTrue(any("[人工介入]" in n["message"] for n in notes))

    def test_small_company_no_intervention(self):
        small = {**QUOTE_FORM, "dev_count": 10}
        resp = self.client.post("/api/enterprise/quote", json=small)
        self.assertIsNone(resp.json()["intervention"])


if __name__ == "__main__":
    unittest.main()
