from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from zhuorui.api.signing import canonical
from zhuorui.api.snapshot import SnapshotError, account_snapshot, cash_balances, security_positions


def response(data):
    return {"code": "000000", "data": data}


def cash_row(currency="USD", account="broker-demo", kind="M"):
    return {
        "moneyType": currency, "fundAccount": account, "accType": kind,
        "cashAmt": Decimal("9999.12"), "enableCash": Decimal("8765.43"),
        "cashBalance": 0,
        "cashAmts": {"usCashAmt": Decimal("123.456789123456789"), "hkCashAmt": Decimal("-4.50"), "cnCashAmt": 0},
    }


def holding(symbol="DEMO", qty=20, cost=Decimal("15.2300")):
    return {"ts": "US", "code": symbol, "market": 2, "type": 1,
            "currentAmount": qty, "enableAmount": 10, "costPrice": cost,
            "last": Decimal("20.80")}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.config = {"account_id": "control-demo", "account_num_id": 8, "trading_enabled": True}
        self.cash = response([cash_row("USD"), cash_row("HKD"), cash_row("CNY")])
        self.holdings = response({"holdList": [holding()]})
        self.account = response({"fundAccount": "broker-demo", "accType": "M"})

    def snapshot(self):
        return account_snapshot(self.config, self.holdings, self.cash, self.account,
                                now=datetime(2026, 9, 10, tzinfo=timezone.utc))

    def test_exact_existing_schema_and_native_currency_cash(self):
        snapshot = self.snapshot()
        self.assertEqual(set(snapshot), {"account_id", "account_num_id", "cash", "cash_by_currency", "positions", "ts", "trading_enabled"})
        self.assertEqual(snapshot["cash"], Decimal("123.456789123456789"))
        self.assertEqual(snapshot["cash_by_currency"], {"USD": Decimal("123.456789123456789"), "HKD": Decimal("-4.50"), "CNH": Decimal(0)})
        self.assertEqual(snapshot["positions"], [{"symbol": "DEMO", "qty": Decimal(20), "avg_price": Decimal("15.2300")}])
        self.assertEqual(snapshot["ts"], "2026-09-10T00:00:00+00:00")

    def test_decimal_serializes_as_exact_json_number(self):
        data = canonical(self.snapshot())
        restored = json.loads(data, parse_float=Decimal)
        self.assertEqual(restored["cash"], Decimal("123.456789123456789"))
        self.assertEqual(restored["positions"][0]["avg_price"], Decimal("15.2300"))

    def test_selected_currency_is_not_summed_or_converted(self):
        self.config["api"] = {"cash_currency": "HKD"}
        self.cash["data"][1]["cashAmts"]["usCashAmt"] = Decimal("400.05")
        self.assertEqual(self.snapshot()["cash"], Decimal("400.05"))

    def test_missing_cash_does_not_become_zero(self):
        for field in ("cashAmts",):
            with self.subTest(field=field):
                source = deepcopy(self.cash)
                del source["data"][0][field]
                with self.assertRaises(SnapshotError):
                    cash_balances(self.config, source)
        for field in ("usCashAmt", "hkCashAmt", "cnCashAmt"):
            with self.subTest(field=field):
                source = deepcopy(self.cash)
                del source["data"][0]["cashAmts"][field]
                with self.assertRaises(SnapshotError):
                    cash_balances(self.config, source)

    def test_cash_row_currency_and_account_ambiguity(self):
        for data in ([], [cash_row("HKD")], [cash_row(), cash_row()], [cash_row(), cash_row(account="other")]):
            with self.subTest(rows=len(data)):
                with self.assertRaises(SnapshotError):
                    cash_balances(self.config, response(data))

    def test_account_response_selects_the_broker_account(self):
        self.cash["data"].insert(0, cash_row(account="other"))
        self.assertEqual(self.snapshot()["cash"], Decimal("123.456789123456789"))

    def test_account_config_must_match_authenticated_account(self):
        for settings in ({"fund_account": "other"}, {"fund_account_type": "C"}):
            self.config["api"] = settings
            with self.assertRaises(SnapshotError):
                self.snapshot()

    def test_configured_account_selection_without_account_response(self):
        self.cash["data"].append(cash_row(account="other"))
        self.config["api"] = {"fund_account": "broker-demo", "fund_account_type": "M"}
        self.assertEqual(cash_balances(self.config, self.cash)["USD"], Decimal("123.456789123456789"))

    def test_failed_or_malformed_responses_rejected(self):
        for data in ({}, response(None), {"code": "000102", "data": []}, response({})):
            with self.subTest(data=data):
                with self.assertRaises(SnapshotError):
                    cash_balances(self.config, data)
        for data in ({}, response({}), response({"holdList": None}), response({"holdList": [None]})):
            with self.subTest(data=data):
                with self.assertRaises(SnapshotError):
                    security_positions(data)

    def test_units_must_be_finite_unformatted_numbers(self):
        for number in (None, True, "1,000", "4K", "2%", " 2 ", Decimal("NaN"), float("inf")):
            with self.subTest(number=str(number)):
                self.holdings["data"]["holdList"] = [holding(qty=number)]
                with self.assertRaises(SnapshotError):
                    self.snapshot()

    def test_empty_positions_and_zero_position_do_not_need_cost(self):
        self.holdings["data"]["holdList"] = []
        self.assertEqual(self.snapshot()["positions"], [])
        self.holdings["data"]["holdList"] = [{"code": "DEMO", "currentAmount": 0}]
        self.assertEqual(self.snapshot()["positions"], [])

    def test_shares_are_current_amount_including_fractional_or_short(self):
        self.holdings["data"]["holdList"] = [holding(qty=Decimal("-2.5"))]
        self.assertEqual(self.snapshot()["positions"][0]["qty"], Decimal("-2.5"))

    def test_duplicate_symbols_are_not_silently_combined(self):
        self.holdings["data"]["holdList"] = [holding(), holding()]
        with self.assertRaises(SnapshotError):
            self.snapshot()

    def test_average_cost_required_for_nonzero_holdings(self):
        for number in (None, "unavailable"):
            self.holdings["data"]["holdList"] = [holding(cost=number)]
            with self.assertRaises(SnapshotError):
                self.snapshot()

    def test_signed_cost_basis_is_preserved(self):
        self.holdings["data"]["holdList"] = [holding(cost=Decimal("-1.25"))]
        self.assertEqual(self.snapshot()["positions"][0]["avg_price"], Decimal("-1.25"))

    def test_nested_control_account_config_and_disabled_trading(self):
        self.config = {"account": {"id": "nested-demo", "numeric_id": "9", "trading_enabled": False}}
        result = self.snapshot()
        self.assertEqual(result["account_id"], "nested-demo")
        self.assertEqual(result["account_num_id"], 9)
        self.assertFalse(result["trading_enabled"])

    def test_invalid_control_identity_rejected(self):
        for value in (True, 0, -1, "1.5", None):
            self.config["account_num_id"] = value
            with self.assertRaises(SnapshotError):
                self.snapshot()


if __name__ == "__main__":
    unittest.main()
