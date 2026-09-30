from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from zoneinfo import ZoneInfo

from zhuorui.api.errors import ApiError
from zhuorui.api.order_snapshot import order_snapshots, order_detail_snapshot
from zhuorui.api.signing import canonical


CONFIG = {"account_id": "control-demo", "account_num_id": 8}
NOW = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)


def milliseconds(stamp):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (stamp - epoch) // timedelta(milliseconds=1)


def order(**changes):
    row = {
        "orderTxnReference": "transaction-demo", "orderNo": "different-number",
        "entrustTime": milliseconds(NOW - timedelta(hours=1)),
        "code": "DEMO", "ts": "US", "entrustBs": "1", "entrustProp": "LO",
        "entrustStatus": "2", "entrustAmount": 100, "businessAmount": 0,
        "entrustPrice": Decimal("200.5000"), "costPrice": Decimal("81.123"),
        "timeInForce": "DAY", "fundAccount": "private-account-demo",
    }
    row.update(changes)
    return row


def response(rows):
    return {"code": "000000", "data": rows}


class OrderSnapshotTests(unittest.TestCase):
    def snapshot(self, row=None, *, now=NOW, config=CONFIG):
        return order_snapshots(config, response([order() if row is None else row]), now=now)[0]

    def test_full_v1_snapshot_identity_numbers_and_observation(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot, {
            "schema_version": 1, "account_id": "control-demo", "account_num_id": 8,
            "order_id": "transaction-demo", "created_at": "2026-09-29T15:00:00+00:00",
            "updated_at": "2026-09-29T16:00:00+00:00", "symbol": "DEMO",
            "side": "BUY", "order_type": "LIMIT", "status": "NEW",
            "quantity": Decimal(100), "filled_quantity": Decimal(0),
            "average_fill_price": None, "limit_price": Decimal("200.5000"),
            "currency": "USD", "time_in_force": "DAY",
        })
        self.assertNotIn("sequence", snapshot)
        encoded = canonical(snapshot)
        self.assertNotIn(b"private-account-demo", encoded)
        self.assertNotIn(b"different-number", encoded)
        decoded = json.loads(encoded, parse_float=Decimal)
        self.assertEqual(decoded["limit_price"], Decimal("200.5000"))

    def test_broker_cost_basis_and_limit_price_are_not_execution_averages(self):
        for row in (order(), order(entrustStatus="8", businessAmount=100)):
            self.assertIsNone(self.snapshot(row)["average_fill_price"])

    def test_complete_execution_list_gives_weighted_average(self):
        fills = [
            {"businessAmount": "20", "businessPrice": "200.125"},
            {"businessAmount": "30", "businessPrice": "200.25"},
        ]
        snapshot = self.snapshot(order(entrustStatus="7", businessAmount=50, bargainList=fills))
        self.assertEqual(snapshot["filled_quantity"], Decimal(50))
        self.assertEqual(snapshot["average_fill_price"], Decimal("200.2"))
        self.assertEqual(snapshot["status"], "PARTIALLY_FILLED")

    def test_incomplete_execution_list_cannot_claim_cumulative_average(self):
        row = order(entrustStatus="7", businessAmount=50,
                    bargainList=[{"businessAmount": 20, "businessPrice": 200}])
        self.assertIsNone(self.snapshot(row)["average_fill_price"])

    def test_detail_supplies_execution_price_for_limit_and_market_orders(self):
        for kind in ("LO", "MO"):
            with self.subTest(kind=kind):
                row = order(entrustProp=kind, entrustStatus="8", businessAmount=100)
                listed = self.snapshot(row)
                detailed = {**row, "bargainList": [
                    {"businessAmount": 100, "businessPrice": "199.9288", "businessBalance": "19992.88"}]}
                detailed.pop("timeInForce")
                detailed.pop("ts")
                snapshot = order_detail_snapshot(CONFIG, {"code": "000000", "data": detailed}, listed, now=NOW)
                self.assertEqual(snapshot["average_fill_price"], Decimal("199.9288"))
                self.assertEqual(snapshot["status"], "FILLED")
                self.assertEqual(snapshot["currency"], "USD")
                self.assertEqual(snapshot["time_in_force"], "DAY")
                self.assertNotEqual(snapshot["average_fill_price"], row["entrustPrice"])
                self.assertNotEqual(snapshot["average_fill_price"], row["costPrice"])

    def test_detail_can_advance_partial_fills_to_complete_with_weighted_average(self):
        row = order(entrustStatus="7", businessAmount=20)
        detailed = {**row, "entrustStatus": "8", "businessAmount": 100, "bargainList": [
            {"businessAmount": 20, "businessPrice": 199},
            {"businessAmount": 80, "businessPrice": 200}]}
        snapshot = order_detail_snapshot(CONFIG, {"code": "000000", "data": detailed}, self.snapshot(row), now=NOW)
        self.assertEqual(snapshot["average_fill_price"], Decimal("199.8"))
        self.assertEqual(snapshot["filled_quantity"], 100)
        self.assertEqual(snapshot["status"], "FILLED")

    def test_detail_rejects_wrong_identity_and_stale_or_incomplete_executions(self):
        row = order(entrustStatus="7", businessAmount=50)
        listed = self.snapshot(row)
        detailed = {**row, "bargainList": [{"businessAmount": 50, "businessPrice": 200}]}
        changes = (
            {"orderTxnReference": "different-order"}, {"entrustTime": row["entrustTime"] - 1},
            {"code": "OTHER"}, {"entrustBs": "2"}, {"businessAmount": 40,
                "bargainList": [{"businessAmount": 40, "businessPrice": 200}]},
            {"bargainList": None}, {"bargainList": []},
            {"bargainList": [{"businessAmount": 20, "businessPrice": 200}]},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ApiError):
                order_detail_snapshot(CONFIG, {"code": "000000", "data": {**detailed, **change}}, listed, now=NOW)
        for result in (None, {}, {"code": "000102", "data": detailed},
                       {"code": "000000"}, {"code": "000000", "data": []}):
            with self.subTest(result=result), self.assertRaises(ApiError):
                order_detail_snapshot(CONFIG, result, listed, now=NOW)

    def test_detail_cannot_reopen_terminal_order_with_same_cumulative_fill(self):
        for state in ("6", "F"):
            with self.subTest(state=state):
                row = order(entrustStatus=state, businessAmount=50)
                detailed = {**row, "entrustStatus": "7", "bargainList": [
                    {"businessAmount": 50, "businessPrice": 200}]}
                with self.assertRaisesRegex(ApiError, "stale order status"):
                    order_detail_snapshot(CONFIG, {"code": "000000", "data": detailed}, self.snapshot(row), now=NOW)

    def test_nonterminating_weighted_average_is_finite_and_serializable(self):
        row = order(entrustStatus="7", businessAmount=3, bargainList=[
            {"businessAmount": 1, "businessPrice": 1},
            {"businessAmount": 2, "businessPrice": 2},
        ])
        snapshot = self.snapshot(row)
        self.assertTrue(snapshot["average_fill_price"].is_finite())
        self.assertAlmostEqual(float(snapshot["average_fill_price"]), 5 / 3)
        json.loads(canonical(snapshot))

    def test_today_uses_eastern_creation_date_at_summer_midnight(self):
        observed = datetime(2026, 9, 29, 4, 5, tzinfo=timezone.utc)
        rows = [
            order(orderTxnReference="previous", entrustTime=milliseconds(observed.replace(hour=3, minute=59))),
            order(orderTxnReference="today", entrustTime=milliseconds(observed.replace(minute=0))),
            order(orderTxnReference="future", entrustTime=milliseconds(observed + timedelta(days=1))),
        ]
        snapshots = order_snapshots(CONFIG, response(rows), now=observed)
        self.assertEqual([item["order_id"] for item in snapshots], ["today"])

    def test_winter_midnight_has_five_hour_utc_offset(self):
        observed = datetime(2026, 1, 10, 5, 5, tzinfo=timezone.utc)
        rows = [
            order(orderTxnReference="previous", entrustTime=milliseconds(observed.replace(hour=4, minute=59))),
            order(orderTxnReference="today", entrustTime=milliseconds(observed.replace(minute=0))),
        ]
        snapshots = order_snapshots(CONFIG, response(rows), now=observed)
        self.assertEqual([item["order_id"] for item in snapshots], ["today"])

    def test_repeated_dst_hour_uses_instant_and_keeps_both_orders(self):
        ny = ZoneInfo("America/New_York")
        observed = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        rows = [order(orderTxnReference=f"fold-{fold}", entrustTime=milliseconds(
            datetime(2026, 11, 1, 1, 30, fold=fold, tzinfo=ny))) for fold in (0, 1)]
        snapshots = order_snapshots(CONFIG, response(rows), now=observed)
        self.assertEqual([row["created_at"] for row in snapshots], [
            "2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00"])

    def test_spring_dst_transition_does_not_skip_orders(self):
        observed = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
        rows = [order(orderTxnReference=f"hour-{hour}", entrustTime=milliseconds(
            observed.replace(hour=hour, minute=30))) for hour in (6, 7)]
        self.assertEqual(len(order_snapshots(CONFIG, response(rows), now=observed)), 2)

    def test_created_at_is_immutable_as_observation_advances(self):
        first = self.snapshot()
        second = self.snapshot(now=NOW + timedelta(minutes=2))
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertNotEqual(first["updated_at"], second["updated_at"])

    def test_terminal_and_non_us_orders_are_included(self):
        rows = [
            order(orderTxnReference="filled", entrustStatus="8", businessAmount=100),
            order(orderTxnReference="canceled", entrustStatus="5", businessAmount=40),
            order(orderTxnReference="expired", entrustStatus="G", businessAmount=40),
            order(orderTxnReference="rejected", entrustStatus="9", ts="HK", entrustBs="2"),
        ]
        snapshots = order_snapshots(CONFIG, response(rows), now=NOW)
        self.assertEqual([row["status"] for row in snapshots], ["FILLED", "CANCELED", "EXPIRED", "REJECTED"])
        self.assertEqual(snapshots[-1]["side"], "SELL")
        self.assertEqual(snapshots[-1]["currency"], "HKD")

    def test_hs_and_rw_states(self):
        groups = {
            "PENDING_NEW": ("0", "1", "H", "ACK", "PENDING_NEW"),
            "NEW": ("2", "NEW", "REPLACED"),
            "PENDING_CANCEL": ("3", "4", "PENDING_CANCEL"),
            "CANCELED": ("5", "6", "CANCELED"),
            "EXPIRED": ("F", "G"), "REJECTED": ("9", "J", "REJECTED"),
        }
        for expected, states in groups.items():
            for state in states:
                with self.subTest(state=state):
                    row = order(entrustStatus=state, businessAmount=20 if state in {"4", "5", "G"} else 0)
                    self.assertEqual(self.snapshot(row)["status"], expected)
        for state in ("7", "E", "PARTIALLY_FILLED"):
            self.assertEqual(self.snapshot(order(entrustStatus=state, businessAmount=20))["status"], "PARTIALLY_FILLED")
        for state in ("8", "FILLED"):
            self.assertEqual(self.snapshot(order(entrustStatus=state, businessAmount=100))["status"], "FILLED")

    def test_unknown_and_unrepresentable_status_is_described(self):
        for state in ("future_status", "A", "PENDING_REPLACE", "SUSPENDED", "DONE_FOR_DAY", None):
            with self.subTest(state=state):
                snapshot = self.snapshot(order(entrustStatus=state, remark="Source reason"))
                self.assertEqual(snapshot["status"], "UNKNOWN")
                self.assertIn("Unmapped broker order status:", snapshot["message"])
                self.assertIn("Source reason", snapshot["message"])

    def test_accepted_order_cumulative_partial_execution_is_preserved(self):
        snapshot = self.snapshot(order(businessAmount=20))
        self.assertEqual(snapshot["status"], "PARTIALLY_FILLED")

    def test_no_defaults_for_missing_tif_currency_and_other_type(self):
        row = order(entrustProp="MIT", ts="other")
        del row["timeInForce"]
        snapshot = self.snapshot(row)
        self.assertEqual(snapshot["order_type"], "OTHER")
        self.assertIn("Broker order type: MIT.", snapshot["message"])
        self.assertNotIn("time_in_force", snapshot)
        self.assertNotIn("currency", snapshot)

    def test_stop_and_trailing_types_and_price_fields(self):
        for kind, expected in (("STP", "STOP"), ("STL", "STOP_LIMIT"),
                               ("STOP_LIMIT", "STOP_LIMIT"), ("TS", "TRAILING_STOP"),
                               ("TSL", "TRAILING_STOP")):
            with self.subTest(kind=kind):
                snapshot = self.snapshot(order(entrustProp=kind, stopPrice="199.1"))
                self.assertEqual(snapshot["order_type"], expected)
                self.assertEqual(snapshot["stop_price"], Decimal("199.1"))
        snapshot = self.snapshot(order(entrustProp="MO"))
        self.assertEqual(snapshot["order_type"], "MARKET")
        self.assertNotIn("limit_price", snapshot)

    def test_missing_required_prices_fail(self):
        for kind, field in (("LO", "entrustPrice"), ("STP", "stopPrice"), ("STL", "stopPrice")):
            row = order(entrustProp=kind)
            row.pop(field, None)
            with self.subTest(kind=kind), self.assertRaises(ApiError):
                self.snapshot(row)

    def test_empty_success_encodings(self):
        for source in ({"code": "000000"}, response(None), response([])):
            self.assertEqual(order_snapshots(CONFIG, source, now=NOW), [])
            self.assertEqual(order_snapshots({}, source, now=NOW), [])

    def test_failed_and_malformed_response_not_empty(self):
        for source in (None, {}, {"code": "failed"}, response({}), response(""), response([None])):
            with self.subTest(source=source), self.assertRaises(ApiError):
                order_snapshots(CONFIG, source, now=NOW)

    def test_stable_reference_required_and_duplicates_rejected(self):
        for reference in (None, "", " padded ", "x" * 257, "bad\nreference"):
            with self.subTest(reference=reference), self.assertRaises(ApiError):
                self.snapshot(order(orderTxnReference=reference))
        with self.assertRaises(ApiError):
            order_snapshots(CONFIG, response([order(), order()]), now=NOW)

    def test_numeric_values_are_finite_unformatted_and_supported(self):
        values = (None, True, "1,000", " 2 ", "2%", Decimal("NaN"), float("inf"), "1e309", "1e-400")
        for field in ("entrustAmount", "businessAmount", "entrustPrice"):
            for value in values:
                with self.subTest(field=field, value=str(value)), self.assertRaises(ApiError):
                    self.snapshot(order(**{field: value}))

    def test_invalid_timestamp_and_observation_rejected(self):
        for value in (None, True, -1, 0, "not-a-time", "1.5", "1e100", float("inf")):
            with self.subTest(value=str(value)), self.assertRaises(ApiError):
                self.snapshot(order(entrustTime=value))
        with self.assertRaises(ApiError):
            self.snapshot(order(entrustTime=milliseconds(NOW + timedelta(seconds=1))))
        with self.assertRaises(ApiError):
            self.snapshot(now=NOW.replace(tzinfo=None))
        with self.assertRaises(ApiError):
            self.snapshot(now="invalid")

    def test_invalid_quantity_and_status_fill_combinations_rejected(self):
        changes = (
            {"entrustAmount": 0}, {"entrustAmount": -1}, {"businessAmount": -1},
            {"businessAmount": 101}, {"entrustStatus": "8", "businessAmount": 99},
            {"entrustStatus": "8", "businessAmount": 0},
            {"entrustStatus": "7", "businessAmount": 0},
            {"entrustStatus": "7", "businessAmount": 100},
            {"entrustStatus": "2", "businessAmount": 100},
            {"entrustStatus": "1", "businessAmount": 1},
            {"entrustStatus": "4", "businessAmount": 0},
            {"entrustStatus": "5", "businessAmount": 100},
            {"entrustStatus": "G", "businessAmount": 0},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ApiError):
                self.snapshot(order(**change))

    def test_invalid_execution_records_are_not_averaged(self):
        lists = ({}, [None], [{"businessAmount": 101, "businessPrice": 2}],
                 [{"businessAmount": -1, "businessPrice": 2}],
                 [{"businessAmount": 1, "businessPrice": "NaN"}],
                 [{"businessAmount": 1, "businessPrice": -1}])
        for fills in lists:
            with self.subTest(fills=fills), self.assertRaises(ApiError):
                self.snapshot(order(entrustStatus="7", businessAmount=50, bargainList=fills))

    def test_invalid_identity_side_symbol_status_market_and_tif(self):
        changes = ({"code": "bad symbol"}, {"code": ""}, {"code": "\ud800"},
                   {"code": "ß" * 40}, {"entrustBs": "3"},
                   {"entrustBs": 1}, {"entrustProp": None}, {"entrustStatus": []},
                   {"ts": []}, {"timeInForce": "x" * 33})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ApiError):
                self.snapshot(order(**change))
        with self.assertRaises(ApiError):
            self.snapshot(config={"account_id": "x" * 129, "account_num_id": 8})

    def test_message_selection_and_truncation(self):
        snapshot = self.snapshot(order(message={"en_US": "English failure", "zh_CN": "other"},
                                       remark="Fallback reason"))
        self.assertEqual(snapshot["message"], "English failure")
        snapshot = self.snapshot(order(message="x" * 3000))
        self.assertEqual(len(snapshot["message"]), 2000)
        snapshot = self.snapshot(order(opRemark="Operation failed", remark="REASON"))
        self.assertEqual(snapshot["message"], "Operation failed")
        self.assertLess(len(canonical({**snapshot, "sequence": 9007199254740991})), 65536)

    def test_prior_day_row_still_needs_valid_creation_time(self):
        with self.assertRaises(ApiError):
            order_snapshots(CONFIG, response([{"entrustTime": "invalid"}]), now=NOW)
        yesterday = {"entrustTime": milliseconds(NOW - timedelta(days=1))}
        self.assertEqual(order_snapshots(CONFIG, response([yesterday]), now=NOW), [])


if __name__ == "__main__":
    unittest.main()
