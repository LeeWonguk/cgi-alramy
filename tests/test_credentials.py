#!/usr/bin/env python3
"""CGV 자격증명 저장의 암호화·해시 회귀 (DB 불필요).

secretbox의 암호화 왕복과 cgv_login의 비밀번호 해시 형식을 고정한다. DB에 붙지
않으므로 어디서나 돈다 — CGV_CRED_KEY를 테스트용으로 심고 store를 건드리지 않는다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# store DB 뒷문을 타지 않도록, import 전에 테스트용 키를 심는다.
from cryptography.fernet import Fernet  # noqa: E402

os.environ["CGV_CRED_KEY"] = Fernet.generate_key().decode("ascii")

import secretbox  # noqa: E402


class TestSecretBox(unittest.TestCase):
    def test_roundtrip(self):
        for text in ["!Dnjsrnrl1", "짧은비번", "a" * 200, "특수!@#$%^&*()_+"]:
            token = secretbox.encrypt(text)
            self.assertIsInstance(token, bytes)
            self.assertNotIn(text.encode(), token, "암호문에 원문이 그대로 보인다")
            self.assertEqual(secretbox.decrypt(token), text)

    def test_decrypt_accepts_memoryview(self):
        # psycopg의 bytea는 memoryview로 올 수 있다.
        token = secretbox.encrypt("!Dnjsrnrl1")
        self.assertEqual(secretbox.decrypt(memoryview(token)), "!Dnjsrnrl1")

    def test_ciphertext_is_nondeterministic(self):
        # Fernet은 매번 다른 IV를 쓴다 — 같은 원문도 암호문이 달라야 한다.
        a = secretbox.encrypt("같은값")
        b = secretbox.encrypt("같은값")
        self.assertNotEqual(a, b)
        self.assertEqual(secretbox.decrypt(a), secretbox.decrypt(b))

    def test_wrong_key_raises(self):
        token = secretbox.encrypt("!Dnjsrnrl1")
        other = Fernet(Fernet.generate_key())
        with self.assertRaises(secretbox.SecretBoxError):
            # 다른 키로 만든 Fernet으로는 못 푼다 — decrypt가 이를 감싼다.
            secretbox.decrypt(other.encrypt(b"x") + b"tampered")

    def test_bad_token_raises(self):
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(b"not-a-valid-token")


if __name__ == "__main__":
    unittest.main()
