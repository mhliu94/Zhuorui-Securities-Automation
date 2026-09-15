"""Offline regressions for cent pricing and the captured session instruction."""
import base64
import json
from decimal import Decimal, localcontext
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from zhuorui.api.cli import parser
from zhuorui.api.client import ApiClient
from zhuorui.api.commands import TradingCommand, decode_command, parse_command
from zhuorui.api.errors import ApiError
from zhuorui.api.orders import plan_order
from zhuorui.api.signing import canonical
from test_api_execution_support import FakeOpener, KEY, SETTINGS, session


CONFIG = {"account_id": "synthetic", "account_num_id": 7}


class OrderPolicyTests(unittest.TestCase):
    def test_prices_round_by_side_for_limit_and_timed_cancel(self):
        cases = (("322.1064", "322.11", "322.10"),
                 ("322.1000", "322.10", "322.10"),
                 ("9.9999", "10.00", "9.99"),
                 ("0.019", "0.02", "0.01"),
                 ("1e2", "100.00", "100.00"))
        for kind in ("limit", "timed-cancel"):
            for original, buy, sell in cases:
                for side, expected in (("buy", buy), ("sell", sell)):
                    with self.subTest(kind=kind, original=original, side=side):
                        body = plan_order("AAPL", side, 2, kind, price=original)["unsigned_body"]
                        self.assertEqual(str(body["entrustPrice"]), expected)
                        self.assertEqual(body["allowPrePost"], "Y")
                        self.assertEqual(body["apStatus"], 0)
                        self.assertNotIn("sessionType", body)

    def test_rounding_uses_decimal_precision_independent_of_context(self):
        with localcontext() as context:
            context.prec = 5
            price = "123456789012345678901234567890.01001"
            buy = plan_order("AAPL", "buy", 2, "limit", price=price)["unsigned_body"]
            sell = plan_order("AAPL", "sell", 2, "limit", price=price)["unsigned_body"]
        self.assertEqual(str(buy["entrustPrice"]), "123456789012345678901234567890.02")
        self.assertEqual(str(sell["entrustPrice"]), "123456789012345678901234567890.01")

    def test_zero_cent_sell_and_unbounded_values_fail_before_transport(self):
        opener = FakeOpener()
        client = ApiClient(session(), SETTINGS, opener=opener)
        for price in ("0.0099", "1e-100", "1e101", "1e-101", "9" * 129):
            with self.subTest(price=price), self.assertRaises(ApiError):
                client.submit_order("AAPL", "sell", 2, "limit", price=price)
        self.assertEqual(opener.calls, [])
        body = plan_order("AAPL", "buy", 2, "limit", price="0.0001")["unsigned_body"]
        self.assertEqual(str(body["entrustPrice"]), "0.01")

    def test_kafka_to_signed_request_uses_rounded_price_and_session_policy(self):
        for kind in ("LIMIT_ORDER", "LIMIT_ORDER_FOK"):
            for side, expected in (("buy", "322.11"), ("sell", "322.10")):
                for override, wire_flag in ((None, "Y"), (True, "Y"), (False, "N")):
                    with self.subTest(kind=kind, side=side, override=override):
                        raw = ('{"id":"test","account_num_id":7,"type":"%s",'
                               '"symbol":"AAPL","side":"%s","qty_shares":2,'
                               '"limit_price":322.1064}' % (kind, side))
                        payload = decode_command(raw)
                        if override is not None:
                            payload["allow_pre_post"] = override
                        command = parse_command(payload, CONFIG)
                        self.assertEqual(command.limit_price, Decimal("322.1064"))
                        opener = FakeOpener()
                        client = ApiClient(session(), SETTINGS, opener=opener, now=lambda: 2000)
                        client.submit_order(command.symbol, command.side, command.quantity,
                                            "timed-cancel" if command.order_type == "fok" else command.order_type,
                                            price=command.limit_price, allow_pre_post=command.allow_pre_post)
                        self.assertEqual(len(opener.calls), 1)
                        body = json.loads(opener.calls[0][0].data, parse_float=Decimal)
                        self.assertEqual(str(body["entrustPrice"]), expected)
                        self.assertEqual(body["allowPrePost"], wire_flag)
                        self.assertNotIn("sessionType", body)
                        signature = body.pop("sign")
                        KEY.public_key().verify(base64.b64decode(signature), canonical(body),
                                                padding.PKCS1v15(), hashes.SHA1())

    def test_explicit_regular_hours_aliases_are_respected(self):
        for alias in ("allow_pre_post", "allowPrePost", "extended_hours", "extendedHours"):
            with self.subTest(alias=alias):
                command = parse_command({"id": "test", "account_num_id": 7,
                    "type": "LIMIT_ORDER", "symbol": "AAPL", "side": "sell",
                    "qty_shares": 2, "price": "322.1064", alias: "N"}, CONFIG)
                self.assertFalse(command.allow_pre_post)

    def test_market_defaults_remain_native_and_regular_hours(self):
        command = TradingCommand("test", "AAPL", "sell", 2, "market", None)
        self.assertFalse(command.allow_pre_post)
        for flag in (None, False):
            body = plan_order("AAPL", "sell", 2, "market", allow_pre_post=flag)["unsigned_body"]
            self.assertEqual(body["entrustProp"], "MO")
            self.assertNotIn("entrustPrice", body)
            self.assertNotIn("allowPrePost", body)
            self.assertNotIn("sessionType", body)
        with self.assertRaises(ApiError):
            plan_order("AAPL", "sell", 2, "market", allow_pre_post=True)

    def test_session_parameter_requires_a_boolean_at_transport_boundary(self):
        for value in ("N", "Y", 0, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ApiError):
                plan_order("AAPL", "sell", 2, "limit", price="322.10", allow_pre_post=value)

    def test_cli_preview_defaults_match_transport_and_can_select_regular_hours(self):
        for extra, flag in (([], "Y"), (["--allow-pre-post"], "Y"), (["--no-allow-pre-post"], "N")):
            args = parser().parse_args(["plan-order", "AAPL", "sell", "2",
                                        "--type", "limit", "--price", "322.1064", *extra])
            body = plan_order(args.symbol, args.side, args.quantity, args.type,
                              price=args.price, allow_pre_post=args.allow_pre_post)["unsigned_body"]
            self.assertEqual(body["allowPrePost"], flag)
            self.assertEqual(str(body["entrustPrice"]), "322.10")


if __name__ == "__main__":
    unittest.main()
