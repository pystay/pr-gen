"""LLM 客户端单元测试（mock urllib，不发起真实请求）。"""

import json
import unittest
from unittest import mock

from pr_gen import llm

FAKE_KEY = "sk-fake"


def _fake_open(success_body: dict, status: int = 200):
    resp = mock.Mock()
    resp.read.return_value = json.dumps(success_body).encode("utf-8")
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    if status != 200:
        raise llm.urllib.error.HTTPError(
            "url", status, "err", {}, mock.Mock(read=lambda: b"detail")
        )
    return resp


class TestLLMClient(unittest.TestCase):
    def test_missing_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            from pr_gen import paths

            with mock.patch.object(paths, "load_reasonix_keys", return_value={}):
                with self.assertRaises(llm.LLMError):
                    llm.LLMClient(api_key="", api_key_env="DEEPSEEK_API_KEY")

    def test_success(self):
        client = llm.LLMClient(api_key=FAKE_KEY)
        body = {"content": [{"type": "text", "text": " 生成的描述  "}]}
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_open(body)) as m:
            out = client.messages("sys", "user")
        self.assertEqual(out, "生成的描述")
        # 校验请求格式
        req = m.call_args.args[0]
        self.assertIn("/v1/messages", req.full_url)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_KEY}")
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["system"], "sys")

    def test_http_401(self):
        client = llm.LLMClient(api_key=FAKE_KEY)
        with mock.patch("urllib.request.urlopen",
                        side_effect=llm.urllib.error.HTTPError(
                            "url", 401, "Unauthorized", {},
                            mock.Mock(read=lambda: b"bad key"))):
            with self.assertRaises(llm.LLMError) as ctx:
                client.messages("sys", "user")
        self.assertIn("401", str(ctx.exception))

    def test_no_text_block(self):
        client = llm.LLMClient(api_key=FAKE_KEY)
        body = {"content": [{"type": "tool_use", "id": "x"}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_open(body)):
            with self.assertRaises(llm.LLMError):
                client.messages("sys", "user")


if __name__ == "__main__":
    unittest.main()
