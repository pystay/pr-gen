import unittest
from app.auth import hash_password, sign_token, verify_token


class TestAuth(unittest.TestCase):
    def test_hash_password(self):
        self.assertEqual(len(hash_password("x")), 64)

    def test_verify_token(self):
        token = sign_token("alice", "secret")
        self.assertTrue(verify_token(token, "alice", "secret"))
        self.assertFalse(verify_token(token, "bob", "secret"))
        self.assertFalse(verify_token("tampered", "alice", "secret"))
