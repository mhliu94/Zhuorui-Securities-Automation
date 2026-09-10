"""Offline regressions for the decimal scale observed in the Limit capture."""
import json
import unittest
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric import rsa

from probe_public_api import canonical, signature


class DecimalSigningTests(unittest.TestCase):
    def test_captured_style_price_scale_survives_json_parse(self):
        payload = json.loads('{"timeStamp":1234,"entrustPrice":12.3400}', parse_float=Decimal)
        self.assertEqual(canonical(payload), b'{"entrustPrice":12.3400,"timeStamp":1234}')

    def test_numerically_equal_prices_can_have_different_signatures(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        scaled = {"entrustPrice": Decimal("12.3400"), "timeStamp": 1234}
        collapsed = {"entrustPrice": Decimal("12.34"), "timeStamp": 1234}
        self.assertNotEqual(signature(key, scaled), signature(key, collapsed))

    def test_nested_values_are_sorted_without_quoting_decimals(self):
        self.assertEqual(canonical({"z": [{"price": Decimal("1.20"), "allowed": True}], "a": None}),
                         b'{"a":null,"z":[{"allowed":true,"price":1.20}]}')

    def test_existing_integer_only_requests_are_unchanged(self):
        self.assertEqual(canonical({"timeStamp":1234,"market":2}), b'{"market":2,"timeStamp":1234}')

    def test_nonfinite_prices_cannot_be_signed(self):
        for number in (Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf")):
            with self.subTest(number=number), self.assertRaises(ValueError):
                canonical({"entrustPrice":number})


if __name__ == "__main__":
    unittest.main()
