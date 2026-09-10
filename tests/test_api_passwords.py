"""Synthetic password-transform vectors; no login requests or real credentials."""
import unittest

from zhuorui.api.errors import ApiError
from zhuorui.api.passwords import login_password_hash


class LoginPasswordTests(unittest.TestCase):
    def test_known_ascii_md5_vectors_match_login_wire_format(self):
        vectors = {
            "abc": "900150983cd24fb0d6963f7d28e17f72",
            "password": "5f4dcc3b5aa765d61d8327deb882cf99",
            "message digest": "f96b697d7cb7938d525a2f31aaf161d0",
        }
        for password, expected in vectors.items():
            with self.subTest(vector=password):
                actual = login_password_hash(password)
                self.assertEqual(actual, expected)
                self.assertRegex(actual, r"^[0-9a-f]{32}$")

    def test_unicode_password_uses_utf8_bytes(self):
        self.assertEqual(login_password_hash("你好"), "7eca689f0d3389d9dea66ae112e5cfd7")

    def test_password_case_and_whitespace_are_preserved(self):
        values = [login_password_hash(value) for value in ("password", "Password", " password", "password ")]
        self.assertEqual(len(set(values)), 4)

    def test_missing_or_non_text_password_rejected(self):
        for value in (None, "", True, 123456, b"password", ["password"]):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(ApiError):
                    login_password_hash(value)

    def test_malformed_unicode_rejected_with_no_password_in_error(self):
        marker = "synthetic-private-marker"
        with self.assertRaises(ApiError) as caught:
            login_password_hash(marker + "\ud800")
        self.assertNotIn(marker, str(caught.exception))

    def test_length_limit_is_utf8_byte_count(self):
        self.assertEqual(len(login_password_hash("é" * 2048)), 32)
        for value in ("a" * 4097, "é" * 2049):
            with self.assertRaises(ApiError) as caught:
                login_password_hash(value)
            self.assertNotIn(value, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
