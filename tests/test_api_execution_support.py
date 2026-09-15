"""Offline HTTP and persistence checks; never connect to a broker or Kafka."""
import base64
from decimal import Decimal
import http.client
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import urllib.error

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from zhuorui.api.client import ApiClient, NoRedirect
from zhuorui.api.config import ApiSettings
from zhuorui.api.errors import ApiError, BrokerRejected, LoggedInElsewhere, OrderOutcomeUnknown, SessionExpired
from zhuorui.api.journal import CommandJournal, InstanceLock
from zhuorui.api.listener_config import load_listener_settings
from zhuorui.api.signing import canonical


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SETTINGS = ApiSettings(Path("unused-session"), Path("unused-capture"), Path("unused-apk"))
CONFIG = {"account_id": "synthetic-account", "account_num_id": 7, "server_id": "test-server",
          "kafka": {"bootstrap_servers": "unused.invalid:9092", "group_id": "shared-ui-group"}}


def session():
    return {"headers": {"token": "synthetic-token", "userid": "test-user", "deviceid": "test-device",
                        "host": "should-not-be-used.invalid", "Content-Length": "123456"},
            "signing_key": base64.b64encode(KEY.private_bytes(serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode()}


class Reply(io.BytesIO):
    def __init__(self, raw, status=200):
        super().__init__(raw)
        self.status = status


class FakeOpener:
    def __init__(self, raw=b'{"code":"000000","data":{"orderTxnReference":"synthetic-ref"}}',
                 *, status=200, error=None, response_factory=None):
        self.raw, self.status, self.error = raw, status, error
        self.response_factory = response_factory
        self.calls = []

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if self.error:
            raise self.error
        return self.response_factory() if self.response_factory else Reply(self.raw, self.status)


class ApiWriteTransportTests(unittest.TestCase):
    def client(self, **kwargs):
        opener = FakeOpener(**kwargs)
        return ApiClient(session(), SETTINGS, opener=opener, now=lambda: 2000.123), opener

    def test_buy_and_sell_market_use_true_mo_without_price_or_fok_or_extended_hours(self):
        for side, bs in (("buy", "1"), ("sell", "2")):
            with self.subTest(side=side):
                client, opener = self.client()
                client.submit_order("bili", side, 3, "market")
                self.assertEqual(len(opener.calls), 1)
                request, options = opener.calls[0]
                payload = json.loads(request.data, parse_float=Decimal)
                self.assertEqual(payload["entrustProp"], "MO")
                self.assertEqual(payload["entrustBs"], bs)
                self.assertEqual(payload["entrustAmount"], 3)
                self.assertEqual(payload["code"], "BILI")
                self.assertNotIn("entrustPrice", payload)
                self.assertNotIn("allowPrePost", payload)
                self.assertNotIn("timeInForce", payload)
                self.assertEqual(payload["timeStamp"], 2000123)
                self.assertEqual(options, {"timeout": 10.0})
                self.assertEqual(request.full_url, "https://backendpro.zr.hk/as_trade/api/order/v1/entrust_enter")
                self.assertNotIn("Host", request.headers)
                self.assertNotIn("Content-length", request.headers)
                encoded_signature = payload.pop("sign")
                KEY.public_key().verify(base64.b64decode(encoded_signature), canonical(payload),
                                        padding.PKCS1v15(), hashes.SHA1())

    def test_limit_and_timed_cancel_both_use_lo_with_cent_price(self):
        for kind in ("limit", "timed-cancel"):
            with self.subTest(kind=kind):
                client, opener = self.client()
                client.submit_order("BILI", "sell", 2, kind, price=Decimal("25.1000"), allow_pre_post=True)
                body = opener.calls[0][0].data
                payload = json.loads(body, parse_float=Decimal)
                self.assertEqual(payload["entrustProp"], "LO")
                self.assertEqual(payload["entrustBs"], "2")
                self.assertEqual(payload["allowPrePost"], "Y")
                self.assertIn(b'"entrustPrice":25.10', body)
                self.assertNotIn("timeInForce", payload)
                self.assertNotIn(b'"FOK"', body)

    def test_cancel_wire_body_contains_only_reference_timestamp_and_signature(self):
        client, opener = self.client()
        client.cancel_order("synthetic-ref")
        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(set(payload), {"orderTxnReference", "timeStamp", "sign"})
        self.assertEqual(payload["orderTxnReference"], "synthetic-ref")

    def test_invalid_order_fails_before_transport(self):
        client, opener = self.client()
        for call in (lambda: client.submit_order("BILI", "buy", 1, "market", price="20"),
                     lambda: client.submit_order("BILI", "buy", 1, "fok", price="20"),
                     lambda: client.cancel_order("")):
            with self.assertRaises(ApiError):
                call()
        self.assertEqual(opener.calls, [])

    def test_timeout_http_failure_and_transport_errors_are_unknown_and_never_retried(self):
        errors = (TimeoutError("private transport detail"), urllib.error.URLError("private detail"),
                  OSError("private detail"),
                  urllib.error.HTTPError("https://unused.invalid", 500, "private detail", {}, None))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client, opener = self.client(error=error)
                with self.assertRaises(OrderOutcomeUnknown) as caught:
                    client.submit_order("BILI", "buy", 1, "market")
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(opener.calls), 1)

    def test_unexpected_status_invalid_json_shape_and_code_are_unknown_without_retry(self):
        cases = ({"status": 503}, {"raw": b'{"code":"000000"'}, {"raw": b'[]'},
                 {"raw": b'{"code":0}'}, {"raw": b'{"code":"x"}'}, {"raw": b'{}'},
                 {"raw": b'{"code":"000001","code":"000000"}'},
                 {"raw": b'{"code":"000000","data":{"amount":NaN}}'})
        for case in cases:
            with self.subTest(case=case):
                client, opener = self.client(**case)
                with self.assertRaises(OrderOutcomeUnknown):
                    client.cancel_order("synthetic-ref")
                self.assertEqual(len(opener.calls), 1)

    def test_completed_rejection_and_auth_failures_have_specific_errors_without_retry(self):
        for code, expected in (("000001", BrokerRejected), ("000102", SessionExpired),
                               ("000112", LoggedInElsewhere)):
            with self.subTest(code=code):
                client, opener = self.client(raw=json.dumps({"code": code, "msg": "private broker detail"}).encode())
                with self.assertRaises(expected) as caught:
                    client.submit_order("BILI", "sell", 1, "market")
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIsInstance(caught.exception, OrderOutcomeUnknown)
                self.assertEqual(len(opener.calls), 1)

    def test_redirect_after_write_dispatch_is_an_unknown_outcome(self):
        class RedirectOpener(FakeOpener):
            def open(self, request, **kwargs):
                self.calls.append((request, kwargs))
                return NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://other.invalid")
        opener = RedirectOpener()
        client = ApiClient(session(), SETTINGS, opener=opener)
        with self.assertRaises(OrderOutcomeUnknown):
            client.submit_order("BILI", "buy", 1, "market")
        self.assertEqual(len(opener.calls), 1)

    def test_quote_sizing_requires_exact_fresh_real_time_instrument_and_floors_shares(self):
        row = {"code": "BILI", "ts": "US", "time": 2000000, "last": "23.40",
               "delay": False, "suspension": 1}
        client, opener = self.client(raw=json.dumps({"code": "000000", "data": [row]}).encode())
        self.assertEqual(client.quantity_for_notional("BILI", Decimal("100.00")), 4)
        self.assertEqual(len(opener.calls), 1)
        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(payload["stockVos"], [{"code": "BILI", "ts": "US"}])
        for overrides in ({"delay": True}, {"delay": None}, {"time": 1900000},
                          {"last": "NaN"}, {"last": 0}, {"suspension": True},
                          {"suspension": 2}, {"code": "AAPL"}, {"ts": "HK"}):
            with self.subTest(overrides=overrides):
                client, opener = self.client(raw=json.dumps({"code": "000000", "data": [{**row, **overrides}]}).encode())
                with self.assertRaises(ApiError):
                    client.quantity_for_notional("BILI", Decimal("100.00"))
                self.assertEqual(len(opener.calls), 1)

    def test_quote_nonfinite_number_and_duplicate_result_keys_are_rejected(self):
        for raw in (b'{"code":"000000","data":[{"last":NaN}]}',
                    b'{"code":"000001","code":"000000","data":[]}'):
            with self.subTest(raw=raw):
                client, opener = self.client(raw=raw)
                with self.assertRaises(ApiError):
                    client.quantity_for_notional("BILI", Decimal("100.00"))
                self.assertEqual(len(opener.calls), 1)

    def test_truncated_response_read_is_an_unknown_outcome(self):
        class TruncatedReply(Reply):
            def read(self, *args):
                raise http.client.IncompleteRead(b'partial private detail', 100)
        client, opener = self.client(response_factory=lambda: TruncatedReply(b''))
        with self.assertRaises(OrderOutcomeUnknown) as caught:
            client.submit_order("BILI", "buy", 1, "market")
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(opener.calls), 1)


class ApiJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "commands.sqlite3"
        self.binding = {"account_id": "synthetic-account", "account_num_id": 7, "broker_user_id": "user-7"}
        self.journal = CommandJournal(self.path, self.binding)

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def test_claim_and_state_survive_reopen_and_redelivery_is_not_a_new_claim(self):
        payload = {"symbol": "BILI", "quantity": 1, "kind": "market"}
        self.assertTrue(self.journal.claim("id-1", payload))
        self.journal.update("id-1", "dispatching")
        self.journal.close()
        self.journal = CommandJournal(self.path, self.binding)
        self.assertFalse(self.journal.claim("id-1", payload))
        self.assertEqual(self.journal.get("id-1")["state"], "dispatching")

    def test_reused_id_with_different_economics_is_rejected(self):
        self.journal.claim("id-1", {"symbol": "BILI", "quantity": 1})
        with self.assertRaisesRegex(ApiError, "reused"):
            self.journal.claim("id-1", {"symbol": "BILI", "quantity": 2})
        self.assertIn('"quantity":1', self.journal.get("id-1")["payload"])

    def test_unresolved_states_are_returned_for_reconciliation(self):
        for state in ("received", "dispatching", "unknown", "submitted", "rejected"):
            self.journal.claim(state, {"state": state})
            self.journal.update(state, state)
        self.assertEqual({row["id"] for row in self.journal.unresolved()}, {"dispatching", "unknown"})

    def test_pending_cancellations_keep_reference_and_due_time_across_state_updates(self):
        self.journal.claim("fok-1", {"kind": "timed-cancel"})
        self.journal.update("fok-1", "submitted", reference="synthetic-ref", cancel_due=100,
                            cancel_state="pending")
        self.journal.update("fok-1", "submitted", cancel_state="dispatching")
        rows = self.journal.pending_cancellations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reference"], "synthetic-ref")
        self.assertEqual(rows[0]["cancel_due"], 100)
        self.journal.update("fok-1", "submitted", cancel_state="acknowledged")
        self.assertEqual(self.journal.pending_cancellations(), [])

    def test_journal_cannot_be_rebound_to_another_account_or_broker_user(self):
        for field, value in (("account_id", "other"), ("account_num_id", 8), ("broker_user_id", "user-8")):
            with self.subTest(field=field), self.assertRaisesRegex(ApiError, "another configured account"):
                CommandJournal(self.path, {**self.binding, field: value})
        self.assertTrue(self.journal.claim("still-original", {"quantity": 1}))

    def test_instance_lock_prevents_another_owner_and_releases_on_exit(self):
        path = Path(self.directory.name) / "commands.lock"
        with InstanceLock(path):
            with self.assertRaisesRegex(ApiError, "Another API listener"):
                with InstanceLock(path):
                    self.fail("Second owner acquired the lock")
        with InstanceLock(path):
            pass


class ApiListenerSettingsTests(unittest.TestCase):
    def settings(self, config=None):
        return load_listener_settings(Path("synthetic/config.json"), CONFIG if config is None else config)

    def test_default_holdings_interval_30_and_live_trades_enabled(self):
        settings = self.settings()
        self.assertEqual(settings.holdings_interval_seconds, 30)
        self.assertTrue(settings.live_orders_enabled)
        for value in (False, "false"):
            self.assertFalse(self.settings({**CONFIG, "api": {"live_orders_enabled": value}}).live_orders_enabled)

    def test_default_consumer_groups_are_distinct_for_accounts_even_when_ui_group_shared(self):
        first = self.settings()
        second = self.settings({**CONFIG, "account_num_id": 8, "account_id": "account-8"})
        self.assertNotEqual(first.group_id, second.group_id)
        self.assertEqual(first.group_id, "shared-ui-group.api.account-7")
        self.assertEqual(second.group_id, "shared-ui-group.api.account-8")

    def test_trading_disabled_account_cannot_be_enabled_by_api_flag(self):
        self.assertTrue(self.settings({**CONFIG, "api": {"live_orders_enabled": True}}).live_orders_enabled)
        for extra in ({"trading_enabled": False}, {"account": {"trading_enabled": False}}):
            with self.subTest(extra=extra):
                settings = self.settings({**CONFIG, **extra, "api": {"live_orders_enabled": True}})
                self.assertFalse(settings.live_orders_enabled)

    def test_relative_runtime_paths_follow_configuration_directory(self):
        settings = self.settings({**CONFIG, "api": {"journal_file": "state/commands.db", "state_file": "state/listener.json"}})
        parent = Path("synthetic/config.json").resolve().parent
        self.assertEqual(settings.journal_file, parent / "state/commands.db")
        self.assertEqual(settings.state_file, parent / "state/listener.json")

    def test_invalid_intervals_and_empty_broker_or_server_rejected(self):
        for interval in (0, -1, True, "NaN", "Infinity", "wrong"):
            with self.subTest(interval=interval), self.assertRaises(ApiError):
                self.settings({**CONFIG, "kafka": {**CONFIG["kafka"], "holdings_interval_seconds": interval}})
        for extra in ({"server_id": ""}, {"kafka": {"bootstrap_servers": ""}}):
            with self.subTest(extra=extra), self.assertRaises(ApiError):
                self.settings({**CONFIG, **extra})

    def test_boolean_and_fractional_account_ids_are_not_coerced_to_account_one(self):
        for account_id in (True, 1.5):
            with self.subTest(account_id=account_id), self.assertRaises((ApiError, ValueError)):
                self.settings({**CONFIG, "account_num_id": account_id})


if __name__ == "__main__":
    unittest.main()
