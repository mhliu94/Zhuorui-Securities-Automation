"""Offline Eastern-day order reads and empty-order cancellation handling."""
import base64
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from tests.test_api import FakeOpener, KEY, Reply, SETTINGS, session
from zhuorui.api.client import ApiClient, ORDER_DETAIL_PATH
from zhuorui.api.commands import CancelCommand
from zhuorui.api.errors import ApiError, LoggedInElsewhere, OrderOutcomeUnknown, SessionExpired
from zhuorui.api.execution import CommandExecutor, cancel_references, order_rows
from zhuorui.api.journal import CommandJournal
from zhuorui.api.session import READ_PATHS
from zhuorui.api.signing import canonical


def utc_ms(year, month, day, hour=0):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class OrderDateReadTests(unittest.TestCase):
    def client(self, response=None, *, now=utc_ms(2026, 9, 30, 2) / 1000):
        response = response if response is not None else {"code": "000000", "data": []}
        opener = FakeOpener(response)
        return ApiClient(session(), SETTINGS, opener=opener, now=lambda: now), opener

    def payload(self, opener):
        self.assertEqual(len(opener.calls), 1)
        request, options = opener.calls[0]
        self.assertEqual(request.full_url,
                         "https://backendpro.zr.hk/as_trade/api/order/v1/get_all_entrust")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(options["timeout"], SETTINGS.request_timeout_seconds)
        body = json.loads(request.data)
        signature = base64.b64decode(body.pop("sign"))
        KEY.public_key().verify(signature, canonical(body), padding.PKCS1v15(), hashes.SHA1())
        self.assertIs(type(body["startDate"]), int)
        self.assertIs(type(body["endDate"]), int)
        self.assertEqual(set(body), {"startDate", "endDate", "timeStamp"})
        return body

    def test_eastern_day_crosses_utc_midnight_and_uses_signed_numeric_window(self):
        now = utc_ms(2026, 9, 30, 2)
        client, opener = self.client(now=now / 1000)
        response = client.query_orders_for_date(date(2026, 9, 29))
        self.assertEqual(response, {"code": "000000", "data": []})
        self.assertEqual(self.payload(opener), {
            "startDate": utc_ms(2026, 9, 29, 4),
            "endDate": utc_ms(2026, 9, 30, 4) - 1,
            "timeStamp": now,
        })
        self.assertEqual(READ_PATHS["order-history"],
                         "/as_trade/api/order/v1/get_all_entrust")

    def test_winter_midnight_uses_five_hour_utc_offset(self):
        client, opener = self.client()
        client.query_orders_for_date(date(2026, 1, 15))
        body = self.payload(opener)
        self.assertEqual(body["startDate"], utc_ms(2026, 1, 15, 5))
        self.assertEqual(body["endDate"], utc_ms(2026, 1, 16, 5) - 1)
        self.assertEqual(body["endDate"] - body["startDate"] + 1, 24 * 60 * 60 * 1000)

    def test_dst_transition_windows_use_next_local_midnight(self):
        for day, start, end, hours in (
                (date(2026, 3, 8), utc_ms(2026, 3, 8, 5), utc_ms(2026, 3, 9, 4), 23),
                (date(2026, 11, 1), utc_ms(2026, 11, 1, 4), utc_ms(2026, 11, 2, 5), 25)):
            with self.subTest(day=day):
                client, opener = self.client()
                client.query_orders_for_date(day)
                body = self.payload(opener)
                self.assertEqual(body["startDate"], start)
                self.assertEqual(body["endDate"], end - 1)
                self.assertEqual(body["endDate"] - body["startDate"] + 1,
                                 hours * 60 * 60 * 1000)

    def test_invalid_date_and_datetime_fail_before_transport(self):
        for day in (None, True, 20260929, "2026-09-29", date.max,
                    datetime(2026, 9, 29), datetime(2026, 9, 29, tzinfo=timezone.utc)):
            with self.subTest(day=day):
                client, opener = self.client()
                with self.assertRaises(ApiError):
                    client.query_orders_for_date(day)
                self.assertEqual(opener.calls, [])

    def test_generic_history_query_requires_an_explicit_date(self):
        client, opener = self.client()
        with self.assertRaisesRegex(ApiError, "explicit Eastern date"):
            client.query("order-history")
        self.assertEqual(opener.calls, [])

    def test_expired_session_does_not_retry_or_write(self):
        client, opener = self.client({"code": "000102"})
        with self.assertRaises(SessionExpired):
            client.query_orders_for_date(date(2026, 9, 29))
        self.payload(opener)


class EmptyOrderResponseTests(unittest.TestCase):
    def test_successful_absent_null_and_empty_data_mean_no_orders(self):
        for response in ({"code": "000000", "msg": "ok"},
                         {"code": "000000", "data": None},
                         {"code": "000000", "data": []}):
            with self.subTest(response=response):
                self.assertEqual(order_rows(response), [])
                self.assertEqual(cancel_references(response, CancelCommand("all", cancel_all=True)), [])
                with self.assertRaisesRegex(ApiError, "not uniquely present"):
                    cancel_references(response, CancelCommand("specific", "synthetic-ref"))

    def test_rejected_or_malformed_responses_remain_errors(self):
        for response in (None, [], {}, {"code": "000102"}, {"code": "000001", "data": None},
                         {"code": "000000", "data": {}}, {"code": "000000", "data": ""},
                         {"code": "000000", "data": False}, {"code": "000000", "data": 0},
                         {"code": "000000", "data": [None]}, {"code": "000000", "data": [0]}):
            with self.subTest(response=response):
                with self.assertRaises(ApiError):
                    order_rows(response)

    def test_cancel_all_empty_success_never_dispatches_cancellation(self):
        for response in ({"code": "000000", "msg": "ok"},
                         {"code": "000000", "data": None}):
            with self.subTest(response=response), TemporaryDirectory() as folder:
                client = Mock()
                client.query.return_value = response
                holdings, emit = Mock(), Mock()
                journal = CommandJournal(Path(folder) / "journal.sqlite3", {"account": "synthetic"})
                try:
                    journal.claim("empty-cancel", {"synthetic": True})
                    executor = CommandExecutor(SimpleNamespace(live_orders_enabled=True),
                                               SimpleNamespace(cancel_after_seconds=1), journal,
                                               lambda: client, holdings, emit)
                    executor.cancel_command(client, CancelCommand("empty-cancel", cancel_all=True))
                    self.assertEqual(journal.get("empty-cancel")["state"], "no_action")
                    client.query.assert_called_once_with("orders")
                    client.cancel_order.assert_not_called()
                    holdings.request.assert_not_called()
                    self.assertEqual(emit.call_args.args[1], "no_action")
                finally:
                    journal.close()


class OrderDetailReadTests(unittest.TestCase):
    reference = "synthetic-detail-ref"
    created = utc_ms(2026, 9, 29, 16)
    now = utc_ms(2026, 9, 29, 20)

    def client(self, response=None):
        response = response if response is not None else {
            "code": "000000", "data": {"orderTxnReference": self.reference,
                                      "entrustTime": self.created, "entrustStatus": "8"}}
        opener = FakeOpener(response)
        return ApiClient(session(), SETTINGS, opener=opener, now=lambda: self.now / 1000), opener

    def assert_only_detail_read(self, opener):
        self.assertEqual(len(opener.calls), 1)
        request, options = opener.calls[0]
        self.assertEqual(request.full_url,
                         "https://backendpro.zr.hk/as_trade/api/order/v2/entrust_detail")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(options["timeout"], SETTINGS.request_timeout_seconds)
        return request

    def test_exact_signed_detail_fields_and_read_only_route(self):
        client, opener = self.client()
        response = client.query_order_detail(self.reference, self.created)
        self.assertEqual(response["data"]["entrustStatus"], "8")
        request = self.assert_only_detail_read(opener)
        body = json.loads(request.data)
        signature = base64.b64decode(body.pop("sign"))
        self.assertEqual(body, {"orderTxnReference": self.reference, "entrustTime": self.created,
                                "timeStamp": self.now})
        self.assertIs(type(body["entrustTime"]), int)
        KEY.public_key().verify(signature, canonical(body), padding.PKCS1v15(), hashes.SHA1())
        self.assertNotIn(ORDER_DETAIL_PATH, READ_PATHS.values())
        with self.assertRaisesRegex(ApiError, "Unsupported broker operation"):
            client._request(ORDER_DETAIL_PATH, {}, write=True)
        self.assert_only_detail_read(opener)

    def test_invalid_reference_fails_before_transport(self):
        for reference in (None, True, 123, "", " ref", "ref ", "two refs", "ref\ttext",
                          "ref\ntext", "ref\0text", "ref\x7ftext", "ref\x85text",
                          "ref\u00a0text", "ref\ud800text", "r" * 257):
            with self.subTest(reference=reference):
                client, opener = self.client()
                with self.assertRaisesRegex(ApiError, "transaction reference"):
                    client.query_order_detail(reference, self.created)
                self.assertEqual(opener.calls, [])

    def test_invalid_creation_time_fails_before_transport(self):
        for stamp in (None, True, False, 0, -1, str(self.created), float(self.created),
                      Decimal(self.created), 253402300800000, 1 << 63):
            with self.subTest(stamp=stamp):
                client, opener = self.client()
                with self.assertRaisesRegex(ApiError, "Unix millisecond creation time"):
                    client.query_order_detail(self.reference, stamp)
                self.assertEqual(opener.calls, [])

    def test_supported_timestamp_and_reference_boundaries(self):
        for stamp in (1, 253402300799999):
            with self.subTest(stamp=stamp):
                client, opener = self.client()
                client.query_order_detail("r" * 256, stamp)
                body = json.loads(self.assert_only_detail_read(opener).data)
                self.assertEqual(body["entrustTime"], stamp)
                self.assertEqual(body["orderTxnReference"], "r" * 256)

    def test_generic_detail_name_or_path_requires_typed_fields(self):
        client, opener = self.client()
        for name in ("order-detail", ORDER_DETAIL_PATH):
            with self.subTest(name=name), self.assertRaises(ApiError):
                client.query(name)
        self.assertEqual(opener.calls, [])

    def test_session_failures_do_not_retry_or_authenticate(self):
        for code, exception in (("000102", SessionExpired), ("000112", LoggedInElsewhere)):
            with self.subTest(code=code):
                client, opener = self.client({"code": code})
                with self.assertRaises(exception):
                    client.query_order_detail(self.reference, self.created)
                self.assert_only_detail_read(opener)

    def test_malformed_response_fails_as_read_error_without_retry(self):
        for response in ([], {}, {"code": True}, {"code": 0}, {"code": "invalid"}):
            with self.subTest(response=response):
                client, opener = self.client(response)
                with self.assertRaises(ApiError) as caught:
                    client.query_order_detail(self.reference, self.created)
                self.assertNotIsInstance(caught.exception, OrderOutcomeUnknown)
                self.assert_only_detail_read(opener)

    def test_duplicate_response_keys_are_rejected_without_retry(self):
        client, opener = self.client()
        def reply(request, **options):
            opener.calls.append((request, options))
            return Reply(b'{"code":"000000","data":{},"data":{}}')
        opener.open = reply
        with self.assertRaisesRegex(ApiError, "invalid JSON"):
            client.query_order_detail(self.reference, self.created)
        self.assert_only_detail_read(opener)

    def test_timeout_is_read_error_without_retry(self):
        client, opener = self.client()
        opener.open = Mock(side_effect=TimeoutError("synthetic-private-timeout"))
        with self.assertRaises(ApiError) as caught:
            client.query_order_detail(self.reference, self.created)
        self.assertNotIsInstance(caught.exception, OrderOutcomeUnknown)
        self.assertNotIn("private", str(caught.exception))
        opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
