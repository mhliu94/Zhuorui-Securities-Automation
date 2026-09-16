import unittest
from datetime import datetime, timezone
from decimal import Decimal

from zhuorui.api.commands import (
    CancelCommand, CommandError, TradingCommand, command_fingerprint,
    decode_command, parse_command, validate_command_age,
)


CONFIG = {"account_id": "ACC-ZHUORUI", "account_num_id": 7, "server_id": "zr-1"}


def order(**updates):
    payload = {"id": "producer-123", "type": "MARKET_ORDER", "account_id": "ACC-ZHUORUI",
               "symbol": "BILI", "side": "buy", "qty_shares": 1}
    payload.update(updates)
    return payload


class ApiCommandTests(unittest.TestCase):
    def test_current_ktrader_market_contract(self):
        result = parse_command(order(), CONFIG)
        self.assertEqual(result, TradingCommand("producer-123", "BILI", "buy", 1,
                                               "market", None))

    def test_current_limit_and_fok_contracts(self):
        for name, expected in (("LIMIT_ORDER", "limit"), ("LIMIT_ORDER_FOK", "fok"),
                               ("fillOrKill", "fok"), ("timed-cancel", "fok")):
            with self.subTest(name=name):
                command = parse_command(order(type=name, limit_price="23.4500"), CONFIG)
                self.assertEqual(command.order_type, expected)
                self.assertEqual(str(command.limit_price), "23.4500")

    def test_limit_time_in_force_fok_is_timed_cancel(self):
        command = parse_command(order(type="LIMIT_ORDER", time_in_force="FOK", price=23), CONFIG)
        self.assertEqual(command.order_type, "fok")

    def test_market_price_is_rejected_for_all_ui_aliases(self):
        for field in ("limit_price", "price", "limitPrice", "limit"):
            with self.subTest(field=field), self.assertRaisesRegex(CommandError, "cannot include a limit price"):
                parse_command(order(**{field: "23.4"}), CONFIG)

    def test_notional_market_retains_budget_without_inventing_shares_or_price(self):
        command = parse_command(order(qty_shares=None, notional_usd="100.50"), CONFIG)
        self.assertIsNone(command.quantity)
        self.assertIsNone(command.limit_price)
        self.assertEqual(command.notional_usd, Decimal("100.50"))
        self.assertEqual(command.order_type, "market")

    def test_quantity_wins_when_market_also_contains_notional_as_ui_contract(self):
        command = parse_command(order(notional_usd="100.50"), CONFIG)
        self.assertEqual(command.quantity, 1)

    def test_missing_quantity_or_budget_rejected(self):
        with self.assertRaises(CommandError):
            parse_command(order(qty_shares=None), CONFIG)
        for name in ("LIMIT_ORDER", "LIMIT_ORDER_FOK"):
            with self.subTest(name=name), self.assertRaises(CommandError):
                parse_command(order(type=name, qty_shares=None, notional_usd=100, price=23), CONFIG)

    def test_all_account_routing_aliases(self):
        for field in ("account_id", "accountId", "account", "account_num_id", "accountNumId",
                      "account_numeric_id", "accountNumericId", "account_num", "accountNum"):
            payload = order(account_id=None)
            payload[field] = 7
            with self.subTest(field=field):
                self.assertIsNotNone(parse_command(payload, CONFIG))

    def test_missing_or_unconfigured_account_routing_rejected(self):
        with self.assertRaisesRegex(CommandError, "explicit account"):
            parse_command(order(account_id=None), CONFIG)
        with self.assertRaisesRegex(CommandError, "Configure account"):
            parse_command(order(), {"server_id": "zr-1"})

    def test_mismatching_any_provided_selector_is_ignored(self):
        for overrides in ({"account_id": "another"}, {"account_num_id": 8},
                          {"accountId": "another"}, {"server_id": "zr-2"},
                          {"serverId": "zr-2"}, {"target_server_id": "zr-2"}):
            with self.subTest(overrides=overrides):
                self.assertIsNone(parse_command(order(**overrides), CONFIG))

    def test_matching_and_overridden_server(self):
        self.assertIsNotNone(parse_command(order(server_id="zr-1"), CONFIG))
        self.assertIsNotNone(parse_command(order(server_id="zr-cli"), CONFIG, server_id="zr-cli"))
        config = {**CONFIG, "kafka": {"server_id": "zr-kafka"}}
        self.assertIsNotNone(parse_command(order(server_id="zr-kafka"), config))

    def test_malformed_selectors_rejected(self):
        for value in (True, {"id": 7}, [7]):
            with self.subTest(value=value), self.assertRaises(CommandError):
                parse_command(order(account_id=value), CONFIG)
        with self.assertRaises(CommandError):
            parse_command(order(account_num_id="7.0"), CONFIG)

    def test_nested_account_config_supported(self):
        config = {"account": {"id": "ACC-ZHUORUI", "numeric_id": 7}, "server_id": "zr-1"}
        self.assertIsNotNone(parse_command(order(), config))

    def test_producer_id_aliases_and_deterministic_kafka_fallback(self):
        for field in ("id", "command_id", "commandId", "order_id", "orderId"):
            payload = order(id=None)
            payload[field] = "external-id"
            with self.subTest(field=field):
                self.assertEqual(parse_command(payload, CONFIG).command_id, "external-id")
        payload = order(id=None)
        first = parse_command(payload, CONFIG, message_id="kafka:trading-commands:2:99")
        second = parse_command(payload, CONFIG, message_id="kafka:trading-commands:2:99")
        self.assertEqual(first, second)
        with self.assertRaisesRegex(CommandError, "stable Kafka"):
            parse_command(payload, CONFIG)

    def test_conflicting_aliases_rejected(self):
        for overrides in ({"command_id": "different"}, {"qty": 2}, {"ticker": "AAPL"},
                          {"direction": "sell"}, {"orderType": "LIMIT_ORDER"}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(CommandError, "Conflicting"):
                parse_command(order(**overrides), CONFIG)

    def test_equivalent_aliases_accepted(self):
        self.assertIsNotNone(parse_command(order(qty="1", ticker="bili", direction="BUY",
                                                orderType="market"), CONFIG))

    def test_invalid_quantities_rejected(self):
        for qty in (True, 0, -1, "1.5", 1.0, Decimal("1"), "NaN", "1,00", "1e3"):
            with self.subTest(qty=qty), self.assertRaises(CommandError):
                parse_command(order(qty_shares=qty), CONFIG)
        self.assertEqual(parse_command(order(qty_shares="1,000"), CONFIG).quantity, 1000)

    def test_invalid_prices_rejected(self):
        for price in (True, 0, -1, "NaN", "Infinity", float("inf"), [], "bad", "1e99999999"):
            with self.subTest(price=price), self.assertRaises(CommandError):
                parse_command(order(type="LIMIT_ORDER", price=price), CONFIG)

    def test_cancel_reference_and_legacy_cancel_all_are_distinct(self):
        for field in ("orderTxnReference", "order_txn_reference", "order_reference", "orderReference", "reference"):
            payload = {"id": "cancel-1", "account_id": "ACC-ZHUORUI", "type": "CANCEL_ORDER", field: "broker-ref"}
            with self.subTest(field=field):
                self.assertEqual(parse_command(payload, CONFIG), CancelCommand("cancel-1", "broker-ref", False))
        command = parse_command({"id": "cancel-all", "account_num_id": 7, "action": "CANCEL_ALL_ORDERS"}, CONFIG)
        self.assertEqual(command, CancelCommand("cancel-all", None, True))
        generic = parse_command({"id": "cancel", "account_num_id": 7, "type": "CANCEL_ORDER"}, CONFIG)
        self.assertTrue(generic.cancel_all)

    def test_cancel_id_is_never_guessed_to_be_broker_reference(self):
        command = parse_command({"order_id": "producer-id", "account_num_id": 7, "type": "CANCEL"}, CONFIG)
        self.assertEqual(command.command_id, "producer-id")
        self.assertIsNone(command.order_reference)

    def test_ambiguous_cancellation_rejected(self):
        payload = {"id": "cancel-1", "account_num_id": 7, "type": "CANCEL_ALL", "orderTxnReference": "ref"}
        with self.assertRaises(CommandError):
            parse_command(payload, CONFIG)
        with self.assertRaises(CommandError):
            parse_command(order(action="cancel"), CONFIG)

    def test_extension_flags_and_supported_tif(self):
        command = parse_command(order(type="LIMIT_ORDER", price="23", allowPrePost="Y"), CONFIG)
        self.assertTrue(command.allow_pre_post)
        self.assertTrue(parse_command(order(allow_pre_post=True), CONFIG).allow_pre_post)
        for overrides in ({"time_in_force": "FOK"}, {"tif": "GTC"}):
            with self.subTest(overrides=overrides), self.assertRaises(CommandError):
                parse_command(order(**overrides), CONFIG)

    def test_unsupported_side_symbol_action_and_order_type(self):
        for overrides in ({"symbol": "AAPL;DELETE"}, {"side": "short"}, {"action": "withdraw_money"},
                          {"type": "DELAYED_MARKET_ORDER"}, {"type": "IOC"}):
            with self.subTest(overrides=overrides), self.assertRaises(CommandError):
                parse_command(order(**overrides), CONFIG)

    def test_decode_preserves_price_and_rejects_ambiguous_json(self):
        payload = decode_command(b'{"price":23.4500}')
        self.assertEqual(str(payload["price"]), "23.4500")
        for raw in (b'[]', b'null', b'{"qty":1,"qty":2}', b'{"price":NaN}', b'\xff', b'{'):
            with self.subTest(raw=raw), self.assertRaises(CommandError):
                decode_command(raw)

    def test_fingerprint_detects_changed_economics_not_aliases_ids_or_decimal_scale(self):
        first = parse_command(order(type="LIMIT_ORDER", price="23.40"), CONFIG)
        equivalent = parse_command(order(type="LIMIT_ORDER", price="23.4000", id="another"), CONFIG)
        changed = parse_command(order(type="LIMIT_ORDER", price="23.4001"), CONFIG)
        self.assertEqual(command_fingerprint(first), command_fingerprint(equivalent))
        self.assertNotEqual(command_fingerprint(first), command_fingerprint(changed))
        # Avoid Decimal context rounding collapsing large but different amounts.
        huge1 = parse_command(order(type="LIMIT_ORDER", price="123456789012345678901234567890.01"), CONFIG)
        huge2 = parse_command(order(type="LIMIT_ORDER", price="123456789012345678901234567890.02"), CONFIG)
        self.assertNotEqual(command_fingerprint(huge1), command_fingerprint(huge2))


class ApiCommandAgeTests(unittest.TestCase):
    NOW = datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()

    def check(self, payload, **kwargs):
        validate_command_age(payload, now=self.NOW, max_age_seconds=300, **kwargs)

    def test_missing_payload_time_can_use_kafka_record_time(self):
        self.check({}, kafka_timestamp_ms=int((self.NOW - 2) * 1000))
        with self.assertRaisesRegex(CommandError, "stale"):
            self.check({}, kafka_timestamp_ms=int((self.NOW - 301) * 1000))

    def test_numeric_seconds_milliseconds_and_iso_timezone_supported(self):
        self.check({"timestamp": self.NOW - 5})
        self.check({"timestamp": int((self.NOW - 5) * 1000)})
        self.check({"timestamp": "2026-09-10T00:00:00Z"})

    def test_stale_future_expired_and_invalid_times_rejected(self):
        for payload in ({"timestamp": self.NOW - 301}, {"timestamp": self.NOW + 61},
                        {"expires_at": self.NOW}, {"timestamp": "2026-09-10T00:00:00"},
                        {"timestamp": True}, {"timestamp": "NaN"}):
            with self.subTest(payload=payload), self.assertRaises(CommandError):
                self.check(payload)

    def test_any_stale_alias_prevents_newer_timestamp_from_overriding_it(self):
        with self.assertRaisesRegex(CommandError, "stale"):
            self.check({"timestamp": self.NOW, "created_at": self.NOW - 999})


if __name__ == "__main__":
    unittest.main()
