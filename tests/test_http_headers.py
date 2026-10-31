"""http_client 头部编码与密钥校验的单元测试（离线）。

测试用的密钥一律是假值：不含任何真实凭据，且刻意带上 TESTONLY 标记，
以便仓库密钥扫描（scripts/check_secrets.sh）放行。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter.http_client import HttpClient, encode_headers  # noqa: E402

FAKE_ASCII = "TESTONLY-fake-key-0001"           # 仅测试用假值
FAKE_NON_ASCII = "TESTONLY-中文-测试密钥-0002"   # 仅测试用假值（非 ASCII）


class TestEncodeHeaders(unittest.TestCase):
    def test_ascii_values_untouched(self):
        got = encode_headers({"Authorization": "Bearer " + FAKE_ASCII})
        self.assertEqual(got["Authorization"], "Bearer " + FAKE_ASCII)

    def test_non_ascii_value_becomes_utf8_bytes(self):
        value = "Bearer " + FAKE_NON_ASCII
        got = encode_headers({"Authorization": value})
        self.assertIsInstance(got["Authorization"], bytes)
        self.assertEqual(got["Authorization"].decode("utf-8"), value)

    def test_mixed_headers(self):
        got = encode_headers({"User-Agent": "arbiter/0.1", "X-Tag": "TESTONLY-审计"})
        self.assertEqual(got["User-Agent"], "arbiter/0.1")
        self.assertIsInstance(got["X-Tag"], bytes)


class TestKeyValidation(unittest.TestCase):
    def test_non_ascii_key_accepted(self):
        # 非 ASCII 密钥允许（走 UTF-8 字节头）
        client = HttpClient("http://127.0.0.1:1", FAKE_NON_ASCII)
        self.assertEqual(client.auth_headers()["Authorization"], "Bearer " + FAKE_NON_ASCII)

    def test_whitespace_key_rejected(self):
        with self.assertRaises(ValueError):
            HttpClient("http://127.0.0.1:1", "TESTONLY-fake key with space")

    def test_control_char_key_rejected(self):
        with self.assertRaises(ValueError):
            HttpClient("http://127.0.0.1:1", "TESTONLY-fake\tkey")


if __name__ == "__main__":
    unittest.main(verbosity=2)
