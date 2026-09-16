"""Synthetic HTTP fixtures only: no broker, Kafka or emulator access."""
import base64
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.error

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from zhuorui.api.client import ApiClient
from zhuorui.api.commands import TradingCommand
from zhuorui.api.errors import ApiError
from zhuorui.api.execution import CommandExecutor
from zhuorui.api.journal import CommandJournal
from zhuorui.api.market_data import (NEW_YORK, ORDER_BOOK_PATH, MARKET_STATUS_PATH,
    BestQuote, market_limit_price, parse_session, quantity_at_limit)
from zhuorui.api.session import READ_PATHS
from zhuorui.api.signing import canonical
from tests.test_api_execution_support import KEY, SETTINGS, Reply, session


def et(value):
    return datetime.fromisoformat(value).replace(tzinfo=NEW_YORK).timestamp()


class ExtendedMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = CommandJournal(Path(self.temp.name) / "commands.db", {"test": True})
        self.addCleanup(lambda: self.journal.close())
        self.now = et("2026-09-16T08:00:00")
        self.code = 11
        self.status_override = None
        self.book_results = []
        self.write_error = None
        self.quote_latency = 0
        self.status_latency = 0
        self.requests = []
        self.events = []
        self.holdings = Mock()
        self.client = ApiClient(session(), SETTINGS, opener=self, now=lambda: self.now)
        self.executor = CommandExecutor(SimpleNamespace(live_orders_enabled=True), SETTINGS,
            self.journal, lambda: self.client, self.holdings,
            lambda cmd, state, message, **extra: self.events.append((state, message, extra)),
            now=lambda: self.now, wall=lambda: self.now, sleep=lambda seconds: None)

    def status(self):
        current = datetime.fromtimestamp(self.now, NEW_YORK)
        hour, minute = {11: (4, 0), 4: (9, 30), 12: (16, 0)}.get(self.code, (0, 0))
        stamp = int(current.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp() * 1000)
        return {"code": "000000", "data": [
            {"market": 2, "authProductType": 1, "statusCode": 8, "nowDate": stamp},
            {"market": 2, "authProductType": 0, "statusCode": self.code, "nowDate": stamp, "delay": True}]}

    def book(self):
        # Deliberately unordered and smaller than the requested quantity:
        # this policy uses best level, not a depth walk.
        return {"code": "000000", "data": {"code": "NVDA", "ts": "US", "type": 22,
            "time": datetime.fromtimestamp(self.now, NEW_YORK).strftime("%Y%m%d%H%M%S%f")[:-3],
            "asklist": [{"price": "219", "qty": "100"}, {"price": "215.13", "qty": "1"}],
            "bidlist": [{"price": "211", "qty": "100"}, {"price": "215.11", "qty": "1"}]}}

    def open(self, request, **kwargs):
        self.requests.append(request)
        path = request.full_url.removeprefix("https://backendpro.zr.hk")
        if path == READ_PATHS["account"]:
            result = {"code": "000000", "data": {"clientId": "test-client"}}
        elif path == READ_PATHS["trade-auth"]:
            result = {"code": "000000", "data": {"accountId": "test-client", "userId": "test-user"}}
        elif path == MARKET_STATUS_PATH:
            self.now += self.status_latency
            result = self.status() if self.status_override is None else self.status_override
        elif path == ORDER_BOOK_PATH:
            self.now += self.quote_latency
            result = self.book_results.pop(0) if self.book_results else self.book()
        elif path.endswith("entrust_enter"):
            if self.write_error:
                raise self.write_error
            result = {"code": "000000", "data": {"orderTxnReference": "synthetic-ref"}}
        else:
            raise AssertionError("Unexpected HTTP route: " + path)
        if isinstance(result, Exception):
            raise result
        return Reply(canonical(result))

    def command(self, command_id="command", side="buy", quantity=2, budget=None):
        return TradingCommand(command_id, "NVDA", side, quantity, "market", None, budget)

    def calls(self, path):
        return [request for request in self.requests if request.full_url.endswith(path)]

    def body(self):
        return json.loads(self.calls("entrust_enter")[-1].data, parse_float=Decimal)

    def test_pre_and_post_market_both_sides_sign_one_percent_opposite_best(self):
        for code, hour, name in ((11, 8, "premarket"), (12, 17, "postmarket")):
            for side, price in (("buy", "217.29"), ("sell", "212.95")):
                with self.subTest(session=name, side=side):
                    self.code, self.now = code, et(f"2026-09-16T{hour:02}:00:00")
                    command = self.command(f"{name}-{side}", side)
                    self.executor.execute(command)
                    self.assertEqual(self.events[-1][0], "submitted")
                    payload = self.body()
                    self.assertEqual(payload["entrustProp"], "LO")
                    self.assertEqual(payload["entrustPrice"], Decimal(price))
                    self.assertEqual(payload["allowPrePost"], "Y")
                    self.assertEqual(payload["entrustAmount"], 2)
                    self.assertNotIn("timeInForce", payload)  # Captured default is DAY.
                    audit = json.loads(self.journal.get(command.command_id)["execution"])
                    self.assertEqual(audit["market_session"], name)
                    self.assertEqual(audit["requested_order_type"], "market")
                    self.assertEqual(audit["submitted_order_type"], "limit")
                    self.assertEqual(audit["limit_price"], price)
                    self.assertEqual(audit["quote_attempts"], 1)
                    self.assertEqual(audit, self.events[-1][2]["execution"])
                    original = json.loads(self.journal.get(command.command_id)["payload"])
                    self.assertEqual(original["order_type"], "market")
                    self.assertIsNone(original["limit_price"])
        self.assertEqual(len(self.calls("entrust_enter")), 4)
        for request in self.requests:
            payload = json.loads(request.data, parse_float=Decimal)
            signed = payload.pop("sign")
            KEY.public_key().verify(base64.b64decode(signed), canonical(payload),
                padding.PKCS1v15(), hashes.SHA1())

    def test_regular_market_preserves_native_mo_without_quote_for_explicit_shares(self):
        self.code, self.now = 4, et("2026-09-16T10:00:00")
        self.executor.execute(self.command())
        self.assertEqual(self.body()["entrustProp"], "MO")
        self.assertNotIn("entrustPrice", self.body())
        self.assertNotIn("allowPrePost", self.body())
        self.assertEqual(self.calls(ORDER_BOOK_PATH), [])

    def test_network_failure_retries_quote_once_then_submits_once(self):
        self.book_results = [urllib.error.URLError("synthetic")]
        self.executor.execute(self.command())
        self.assertEqual(self.events[-1][0], "submitted")
        self.assertEqual(len(self.calls(ORDER_BOOK_PATH)), 2)
        self.assertEqual(len(self.calls("entrust_enter")), 1)
        self.assertEqual(self.events[-1][2]["execution"]["quote_attempts"], 2)

    def test_invalid_first_quote_is_replaced_with_new_successful_quote(self):
        stale = self.book()
        stale["data"]["time"] = "20260916074000000"
        self.book_results = [stale]
        self.executor.execute(self.command())
        self.assertEqual(self.events[-1][0], "submitted")
        self.assertEqual(len(self.calls(ORDER_BOOK_PATH)), 2)
        self.assertEqual(self.body()["entrustPrice"], Decimal("217.29"))

    def test_invalid_quotes_reject_after_two_reads_and_never_dispatch(self):
        cases = [
            {"time": "20260916074500000"}, {"time": "20260916080006000"},
            {"time": "202609160800000000"}, {"time": "20260230080000000"},
            {"code": "AAPL"}, {"ts": "HK"}, {"delay": True}, {"asklist": []},
            {"asklist": [{"price": "NaN", "qty": "1"}]},
            {"asklist": [{"price": "Infinity", "qty": "1"}]},
            {"asklist": [{"price": "0", "qty": "1"}]},
            {"asklist": [{"price": "10", "qty": "0"}]},
            {"asklist": [{"price": "1e1000", "qty": "1"}]},
            {"asklist": [{"price": True, "qty": "1"}]},
            {"asklist": [None]},
        ]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                bad = self.book()
                bad["data"].update(changes)
                self.book_results = [bad, deepcopy(bad)]
                before = len(self.calls(ORDER_BOOK_PATH))
                command = self.command(str(index))
                self.executor.execute(command)
                self.assertEqual(self.events[-1][0], "rejected")
                self.assertIn("two attempts", self.events[-1][1])
                self.assertEqual(len(self.calls(ORDER_BOOK_PATH)) - before, 2)
                self.assertEqual(self.journal.get(command.command_id)["state"], "rejected")
        self.assertEqual(self.calls("entrust_enter"), [])
        self.holdings.request.assert_not_called()

    def test_two_network_failures_do_not_fall_back_to_market_or_last_trade(self):
        self.book_results = [urllib.error.URLError("one"), urllib.error.URLError("two")]
        self.executor.execute(self.command())
        self.assertEqual(len(self.calls(ORDER_BOOK_PATH)), 2)
        self.assertEqual(self.events[-1][0], "rejected")
        self.assertEqual(self.calls("entrust_enter"), [])

    def test_session_auth_errors_are_not_retried(self):
        for index, code in enumerate(("000102", "000112")):
            self.book_results = [{"code": code}]
            before = len(self.calls(ORDER_BOOK_PATH))
            self.executor.execute(self.command(str(index)))
            self.assertEqual(self.events[-1][0], "rejected")
            self.assertEqual(len(self.calls(ORDER_BOOK_PATH)) - before, 1)
        self.assertEqual(self.calls("entrust_enter"), [])

    def test_unknown_submission_never_retries_and_duplicate_stays_duplicate(self):
        self.write_error = urllib.error.URLError("lost acknowledgement")
        command = self.command()
        self.executor.execute(command)
        self.assertEqual(self.events[-1][0], "unknown")
        self.assertEqual(json.loads(self.journal.get("command")["execution"])["limit_price"], "217.29")
        self.executor.execute(command)
        self.assertEqual(self.events[-1][0], "duplicate")
        self.executor.execute(self.command("next"))
        self.assertEqual(self.events[-1][0], "blocked")
        self.assertEqual(len(self.calls("entrust_enter")), 1)
        self.assertEqual(len(self.calls(ORDER_BOOK_PATH)), 1)

    def test_closed_halted_unknown_and_non_equities_status_reject_before_quote(self):
        for code in (0, 1, 8, 9, 10, 14, 15, 16, 99):
            self.code = code
            self.executor.execute(self.command(str(code)))
            self.assertEqual(self.events[-1][0], "rejected")
        self.status_override = self.status()
        self.status_override["data"] = self.status_override["data"][:1]
        self.executor.execute(self.command("options-only"))
        self.assertEqual(self.events[-1][0], "rejected")
        self.assertEqual(self.calls(ORDER_BOOK_PATH), [])
        self.assertEqual(self.calls("entrust_enter"), [])

    def test_stale_future_duplicate_and_clock_inconsistent_sessions_reject(self):
        base = self.status()
        fixtures = []
        for stamp in (int(self.now * 1000) + 1000, int((self.now - 86400) * 1000), True):
            bad = deepcopy(base)
            bad["data"][1]["nowDate"] = stamp
            fixtures.append(bad)
        duplicate = deepcopy(base)
        duplicate["data"].append(duplicate["data"][1])
        fixtures.append(duplicate)
        regular_at_eight = deepcopy(base)
        regular_at_eight["data"][1]["statusCode"] = 4
        fixtures.append(regular_at_eight)
        for index, response in enumerate(fixtures):
            self.status_override = response
            self.executor.execute(self.command(str(index)))
            self.assertEqual(self.events[-1][0], "rejected")
        self.assertEqual(self.calls("entrust_enter"), [])

    def test_session_boundary_crossed_during_quote_rejects(self):
        self.now = et("2026-09-16T09:29:59")
        self.quote_latency = 2
        self.executor.execute(self.command())
        self.assertEqual(self.events[-1][0], "rejected")
        self.assertIn("session changed", self.events[-1][1])
        self.assertEqual(self.calls("entrust_enter"), [])

    def test_slow_status_response_expires(self):
        self.status_latency = 31
        self.executor.execute(self.command())
        self.assertEqual(self.events[-1][0], "rejected")
        self.assertEqual(self.calls(ORDER_BOOK_PATH), [])

    def test_quote_from_previous_session_retried_and_rejected(self):
        self.now = et("2026-09-16T04:00:10")
        bad = self.book()
        bad["data"]["time"] = "20260916035955000"
        self.book_results = [bad, deepcopy(bad)]
        self.executor.execute(self.command())
        self.assertEqual(self.events[-1][0], "rejected")
        self.assertEqual(len(self.calls(ORDER_BOOK_PATH)), 2)

    def test_extended_notional_sizing_uses_rounded_limit(self):
        self.executor.execute(self.command(quantity=None, budget=Decimal("650")))
        self.assertEqual(self.body()["entrustAmount"], 2)  # 3 * 217.29 exceeds $650.
        self.assertEqual(self.body()["entrustPrice"], Decimal("217.29"))
        self.executor.execute(self.command("too-small", quantity=None, budget=Decimal("200")))
        self.assertEqual(self.events[-1][0], "rejected")
        self.assertEqual(len(self.calls("entrust_enter")), 1)

    def test_one_sided_book_accepts_usable_opposite_side(self):
        book = self.book()
        del book["data"]["bidlist"]
        self.book_results = [book]
        self.executor.execute(self.command())
        self.assertEqual(self.events[-1][0], "submitted")

    def test_disabled_and_duplicate_do_not_query_again(self):
        self.executor.settings.live_orders_enabled = False
        self.executor.execute(self.command())
        self.executor.settings.live_orders_enabled = True
        self.executor.execute(self.command())
        self.assertEqual(self.events[-1][0], "duplicate")
        self.assertEqual(self.requests, [])


class MarketDataPolicyTests(unittest.TestCase):
    def test_directed_cent_rounding_uses_exact_decimal_even_with_low_context(self):
        for side, value, expected in (
            ("buy", "10.001", "10.11"), ("sell", "10.001", "9.90"),
            ("buy", "100", "101.00"), ("sell", "100", "99.00"),
            ("buy", "0.009", "0.01"), ("buy", "999999999999999999999.99", "1009999999999999999999.99")):
            with self.subTest(side=side, value=value), localcontext() as context:
                context.prec = 6
                self.assertEqual(market_limit_price(BestQuote(Decimal(value), Decimal(1), 0, ""), side), Decimal(expected))
        with self.assertRaises(ApiError):
            market_limit_price(BestQuote(Decimal("0.009"), Decimal(1), 0, ""), "sell")

    def test_notional_floor_does_not_round_up_near_whole_share(self):
        self.assertEqual(quantity_at_limit(Decimal("299.999999999999999999999999999"), Decimal("100")), 2)

    def test_dst_early_close_and_weekend_session_windows(self):
        for date, hour, code, transition, expected in (
            ("2026-01-15", 8, 11, "04:00", "premarket"),
            ("2026-09-16", 17, 12, "16:00", "postmarket"),
            ("2026-11-27", 14, 12, "13:00", "postmarket")):
            now = et(f"{date}T{hour:02}:00:00")
            response = {"data": [{"market": 2, "authProductType": 0, "statusCode": code,
                                  "nowDate": int(et(f"{date}T{transition}:00") * 1000)}]}
            result = parse_session(response, started=now, now=now, max_age=30)
            self.assertEqual(result.name, expected)
        self.assertEqual(datetime.fromtimestamp(et("2026-01-15T08:00:00"), NEW_YORK).utcoffset().total_seconds(), -18000)
        for value in ("2026-09-19T08:00:00", "2026-09-16T03:59:59", "2026-09-16T20:00:00"):
            now = et(value)
            response = {"data": [{"market": 2, "authProductType": 0, "statusCode": 11,
                                  "nowDate": int(et(value[:10] + "T04:00:00") * 1000)}]}
            with self.assertRaises(ApiError):
                parse_session(response, started=now, now=now, max_age=30)

    def test_old_journal_schema_migrates_without_replaying_existing_command(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE commands (id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, reference TEXT, cancel_due REAL, cancel_state TEXT, message TEXT)")
            db.execute("INSERT INTO commands VALUES ('existing','digest','{}','submitted',1,2,'old-ref',NULL,NULL,NULL)")
            db.commit()
            db.close()
            for _ in range(2):
                journal = CommandJournal(path, {"test": True})
                try:
                    self.assertFalse(journal.claim("existing", {}, semantic_digest="digest"))
                    row = journal.get("existing")
                    self.assertEqual(row["reference"], "old-ref")
                    self.assertEqual(row["state"], "submitted")
                    self.assertIsNone(row["execution"])
                finally:
                    journal.close()


if __name__ == "__main__":
    unittest.main()
