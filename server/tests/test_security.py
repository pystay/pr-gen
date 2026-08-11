"""安全模块单元测试：签名校验、加密、JWT。"""

import unittest

from app.security import (
    Encryptor, admin_token, create_github_app_jwt, decode_jwt,
    verify_webhook_signature,
)

SECRET = "s3cret"


def _make_rsa_key() -> str:
    """动态生成测试用 RSA 私钥（PEM）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class TestWebhookSignature(unittest.TestCase):
    def test_valid_signature(self):
        body = b'{"hello":"world"}'
        import hashlib
        import hmac

        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(
            verify_webhook_signature(SECRET, body, f"sha256={digest}")
        )

    def test_invalid_signature(self):
        self.assertFalse(
            verify_webhook_signature(SECRET, b"body", "sha256=deadbeef")
        )

    def test_missing_or_wrong_prefix(self):
        self.assertFalse(verify_webhook_signature(SECRET, b"body", None))
        self.assertFalse(verify_webhook_signature(SECRET, b"body", "md5=abc"))


class TestEncryptor(unittest.TestCase):
    def test_roundtrip(self):
        key = Encryptor.generate_key()
        enc = Encryptor(key)
        cipher = enc.encrypt("星辰科技 / cto@example.com")
        self.assertNotIn("星辰", cipher)  # 密文不含明文
        self.assertEqual(enc.decrypt(cipher), "星辰科技 / cto@example.com")

    def test_empty(self):
        enc = Encryptor(Encryptor.generate_key())
        self.assertEqual(enc.encrypt(""), "")
        self.assertEqual(enc.decrypt(""), "")

    def test_wrong_key_fails(self):
        enc = Encryptor(Encryptor.generate_key())
        cipher = enc.encrypt("secret data")
        other = Encryptor(Encryptor.generate_key())
        with self.assertRaises(ValueError):
            other.decrypt(cipher)


class TestJWT(unittest.TestCase):
    def test_github_app_jwt(self):
        rsa_key = _make_rsa_key()
        token = create_github_app_jwt("12345", rsa_key, ttl_minutes=9)
        payload = decode_jwt(token)
        self.assertEqual(payload["iss"], "12345")
        self.assertGreater(payload["exp"] - payload["iat"], 0)
        self.assertLessEqual(payload["exp"] - payload["iat"], 600)

    def test_missing_config(self):
        with self.assertRaises(ValueError):
            create_github_app_jwt("", _make_rsa_key())


class TestAdminToken(unittest.TestCase):
    def test_stable_and_distinct(self):
        self.assertEqual(admin_token("a"), admin_token("a"))
        self.assertNotEqual(admin_token("a"), admin_token("b"))


if __name__ == "__main__":
    unittest.main()
