"""Synthetic quote transport tests; no broker, Kafka or emulator operations."""
import base64
from dataclasses import replace
from decimal import Decimal
import json
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from tests.test_api import FakeOpener, KEY, SETTINGS, session
from zhuorui.api.client import ApiClient, QUOTE_PATH
from zhuorui.api.errors import ApiError
from zhuorui.api.signing import canonical


NOW = 1_800_000_000


def quote(**changes):
    row = {"code": "DEMO", "ts": "US", "last": "12.50", "time": NOW * 1000,
           "delay": False, "suspension": 1}
    row.update(changes)
    return row


class ApiQuoteTests(unittest.TestCase):
    def client(self, rows=None, *, response=None, settings=SETTINGS, now=None):
        body = response if response is not None else {"code": "000000", "data": rows if rows is not None else [quote()]}
        opener = FakeOpener(body)
        client = ApiClient(session(), settings, opener=opener, now=now or (lambda: NOW))
        return client, opener

    def assert_only_quote_read(self, opener):
        self.assertEqual(len(opener.calls), 1)
        request, _ = opener.calls[0]
        self.assertEqual(request.full_url, "https://backendpro.zr.hk" + QUOTE_PATH)
        self.assertNotIn("entrust_enter", request.full_url)

    def test_fresh_quote_floors_whole_shares_and_sends_exact_signed_quote_payload(self):
        client, opener = self.client()
        self.assertEqual(client.quantity_for_notional("DEMO", Decimal("99.99")), 7)
        self.assert_only_quote_read(opener)
        request, options = opener.calls[0]
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data)
        signature = base64.b64decode(payload.pop("sign"))
        self.assertEqual(payload, {"stockVos": [{"code": "DEMO", "ts": "US"}], "timeStamp": NOW * 1000})
        self.assertEqual(options["timeout"], SETTINGS.request_timeout_seconds)
        KEY.public_key().verify(signature, canonical(payload), padding.PKCS1v15(), hashes.SHA1())

    def test_decimal_reference_price_has_no_binary_float_rounding(self):
        client, opener = self.client([quote(last=0.1)])
        self.assertEqual(client.quantity_for_notional("DEMO", Decimal("0.3")), 3)
        self.assert_only_quote_read(opener)

    def test_exact_share_cost_and_fractional_remainder_floor(self):
        for budget, expected in (("25.00", 2), ("37.499999", 2), ("37.50", 3)):
            with self.subTest(budget=budget):
                client, _ = self.client()
                self.assertEqual(client.quantity_for_notional("DEMO", Decimal(budget)), expected)

    def test_budget_just_below_one_share_never_rounds_up_through_decimal_context(self):
        client, opener = self.client([quote(last="1")])
        with self.assertRaises(ApiError):
            client.quantity_for_notional("DEMO", Decimal("0.999999999999999999999999999999999999"))
        self.assert_only_quote_read(opener)

    def test_delayed_missing_and_non_boolean_delay_rejected(self):
        for value in (True, None, 0, "false"):
            with self.subTest(value=value):
                client, opener = self.client([quote(delay=value)])
                with self.assertRaisesRegex(ApiError, "delayed"):
                    client.quantity_for_notional("DEMO", 100)
                self.assert_only_quote_read(opener)

    def test_stale_future_wrong_units_and_invalid_timestamp_rejected(self):
        for stamp in (NOW * 1000 - 30_001, NOW * 1000 + 5_001, NOW, None, True,
                      str(NOW * 1000), float(NOW * 1000)):
            with self.subTest(stamp=stamp):
                client, opener = self.client([quote(time=stamp)])
                with self.assertRaisesRegex(ApiError, "stale"):
                    client.quantity_for_notional("DEMO", 100)
                self.assert_only_quote_read(opener)

    def test_configured_age_limit_and_five_second_clock_tolerance(self):
        settings = replace(SETTINGS, quote_max_age_seconds=10)
        for offset, accepted in ((-10_000, True), (-10_001, False), (5_000, True), (5_001, False)):
            with self.subTest(offset=offset):
                client, _ = self.client([quote(time=NOW * 1000 + offset)], settings=settings)
                if accepted:
                    self.assertEqual(client.quantity_for_notional("DEMO", 100), 8)
                else:
                    with self.assertRaises(ApiError):
                        client.quantity_for_notional("DEMO", 100)

    def test_quote_can_become_stale_during_response_wait(self):
        times = iter([NOW, NOW + 31])
        client, opener = self.client(now=lambda: next(times))
        with self.assertRaisesRegex(ApiError, "stale"):
            client.quantity_for_notional("DEMO", 100)
        self.assert_only_quote_read(opener)

    def test_only_proven_trading_statuses_accepted(self):
        for status in (1, 3):
            client, _ = self.client([quote(suspension=status)])
            self.assertEqual(client.quantity_for_notional("DEMO", 100), 8)
        for status in (0, 2, 4, 5, 6, 8, 9, None, True, "1"):
            with self.subTest(status=status):
                client, opener = self.client([quote(suspension=status)])
                with self.assertRaisesRegex(ApiError, "actively trading"):
                    client.quantity_for_notional("DEMO", 100)
                self.assert_only_quote_read(opener)

    def test_invalid_and_nonpositive_reference_prices_rejected(self):
        for price in (None, True, 0, -1, "NaN", "Infinity", "-Infinity", "unavailable", [], {}):
            with self.subTest(price=price):
                client, opener = self.client([quote(last=price)])
                with self.assertRaisesRegex(ApiError, "Invalid quote"):
                    client.quantity_for_notional("DEMO", 100)
                self.assert_only_quote_read(opener)

    def test_invalid_and_nonpositive_budgets_rejected(self):
        for budget in (None, True, 0, -1, Decimal("NaN"), Decimal("Infinity"), "unavailable"):
            with self.subTest(budget=str(budget)):
                client, opener = self.client()
                with self.assertRaisesRegex(ApiError, "Invalid quote"):
                    client.quantity_for_notional("DEMO", budget)
                self.assert_only_quote_read(opener)

    def test_notional_below_one_share_rejected(self):
        client, opener = self.client()
        with self.assertRaisesRegex(ApiError, "below one share"):
            client.quantity_for_notional("DEMO", Decimal("12.49"))
        self.assert_only_quote_read(opener)

    def test_quote_must_identify_exactly_one_matching_us_symbol(self):
        for rows in ([], [quote(code="OTHER")], [quote(ts="HK")], [quote(), quote()], [None], ["DEMO"]):
            with self.subTest(rows=rows):
                client, opener = self.client(rows)
                with self.assertRaisesRegex(ApiError, "exactly one"):
                    client.quantity_for_notional("DEMO", 100)
                self.assert_only_quote_read(opener)

    def test_other_symbols_do_not_supply_the_sizing_price(self):
        client, opener = self.client([quote(code="OTHER", last="0.01"), quote()])
        self.assertEqual(client.quantity_for_notional("DEMO", 100), 8)
        self.assert_only_quote_read(opener)

    def test_missing_or_malformed_response_data_rejected(self):
        for response in ({"code": "000000"}, {"code": "000000", "data": None},
                         {"code": "000000", "data": {}}, {"code": "000000", "data": "DEMO"}):
            with self.subTest(response=response):
                client, opener = self.client(response=response)
                with self.assertRaises(ApiError):
                    client.quantity_for_notional("DEMO", 100)
                self.assert_only_quote_read(opener)

    def test_broker_rejection_cannot_fall_back_to_a_price(self):
        client, opener = self.client(response={"code": "000001", "data": [quote()]})
        with self.assertRaises(ApiError):
            client.quantity_for_notional("DEMO", 100)
        self.assert_only_quote_read(opener)

    def test_invalid_symbols_rejected_without_any_transport(self):
        for symbol in ("demo", "", None, "DEMO?next=order", "DEMO/US", "A" * 17):
            with self.subTest(symbol=symbol):
                client, opener = self.client()
                with self.assertRaises(ApiError):
                    client.quantity_for_notional(symbol, 100)
                self.assertEqual(opener.calls, [])


if __name__ == "__main__":
    unittest.main()
