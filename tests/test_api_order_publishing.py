"""Offline integration of holdings refreshes, broker orders and durable Kafka snapshots."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from zhuorui.api.errors import ApiError, SessionExpired
from zhuorui.api.listener import ClientProvider
from zhuorui.api.listener_config import load_listener_settings
from zhuorui.api.publishing import HoldingsPublisher, ListenerState
from zhuorui.api.signing import canonical


NOW = datetime(2026, 9, 29, 20, tzinfo=timezone.utc)


def order(reference="broker-order", *, status="2", created=None, filled=0):
    return {"orderTxnReference": reference, "orderNo": "display-order", "ts": "US", "code": "DEMO",
            "entrustTime": int((created or NOW - timedelta(hours=4)).timestamp() * 1000),
            "entrustStatus": status, "entrustProp": "LO", "entrustBs": "1",
            "entrustAmount": 10, "businessAmount": filled, "entrustPrice": 12.25,
            "costPrice": 11.50}


class OrderPublishingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.config = {"account_id": "synthetic-account", "account_num_id": 7, "server_id": "test",
                       "kafka": {"bootstrap_servers": "unused.invalid:9092"},
                       "api": {"journal_file": str(self.root / "commands.sqlite3")}}
        self.settings = load_listener_settings(self.root / "config.json", self.config)
        self.state = ListenerState(self.root / "state.json", last_error=None)
        self.current, self.history = [order()], []
        self.client = Mock()
        self.client.query.side_effect = lambda name: {"code": "000000", "data": self.current if name == "orders" else {}}
        self.client.query_orders_for_date.side_effect = lambda day: {"code": "000000", "data": self.history}
        self.producer = Mock()
        self.publisher = self.make_publisher()
        self.addCleanup(self.publisher.close)
        self.enterContext(patch("zhuorui.api.snapshot.account_snapshot",
                                return_value={"account_id": "synthetic-account", "account_num_id": 7}))
        self.enterContext(patch("builtins.print"))

    def make_publisher(self, provider=None):
        return HoldingsPublisher(self.config, self.settings, self.producer,
                                 provider or (lambda: self.client), self.state, wall=lambda: NOW.timestamp())

    def events(self):
        return [call for call in self.producer.send.call_args_list if call.args[0] == "order-status"]

    def test_every_refresh_queries_and_publishes_full_order_state(self):
        for reason in ("periodic", "order_submission", "order_cancellation", "login_recovery"):
            self.publisher.publish(reason)
        self.assertEqual([call.args[0] for call in self.client.query.call_args_list],
                         ["account", "cash", "holdings", "orders"] * 4)
        self.assertEqual(self.client.query_orders_for_date.call_count, 4)
        self.client.query_orders_for_date.assert_called_with(NOW.date())
        self.assertEqual(len(self.events()), 4)
        event = self.events()[0].args[1]
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["status"], "NEW")
        self.assertEqual(event["account_num_id"], 7)
        self.assertIsNone(event["average_fill_price"])
        self.assertEqual(self.events()[0].kwargs["key"], b'["synthetic-account","broker-order"]')
        self.assertTrue(all(canonical(call.args[1]) == canonical(event) for call in self.events()))
        self.assertEqual(self.publisher.published, 4)
        self.assertEqual(self.publisher.orders_published, 4)
        self.assertEqual(self.state.values["last_orders_count"], 1)

    def test_empty_success_has_no_order_message_or_sequence_file(self):
        self.client.query.side_effect = lambda name: {"code": "000000"}
        self.client.query_orders_for_date.side_effect = None
        self.client.query_orders_for_date.return_value = {"code": "000000", "data": None}
        self.publisher.publish("periodic")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.publisher.published, 1)
        self.assertEqual(self.state.values["last_orders_count"], 0)
        self.assertIsNone(self.state.values["last_error"])
        self.assertFalse(self.settings.order_snapshot_journal_file.exists())
        self.assertNotIn("last_orders_publish", self.state.values)

    def test_broker_rejected_order_still_publishes_a_broker_snapshot(self):
        self.current = []
        self.history = [order("broker-rejected-order", status="9")]
        self.publisher.publish("order_submission")
        self.assertEqual(len(self.events()), 1)
        event = self.events()[0].args[1]
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["status"], "REJECTED")
        self.assertEqual(event["order_id"], "broker-rejected-order")
        self.assertNotIn("command_id", event)
        self.assertEqual(self.events()[0].kwargs["key"], b'["synthetic-account","broker-rejected-order"]')

    def test_history_includes_completed_orders_and_excludes_other_eastern_dates(self):
        self.current = []
        self.history = [order(status="8", filled=10), order("old", created=NOW - timedelta(days=1)),
                        order("future", created=NOW + timedelta(days=1))]
        self.publisher.publish("periodic")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0].args[1]["status"], "FILLED")

    def test_current_state_wins_history_overlap_and_duplicates_fail(self):
        calls = Mock()
        calls.attach_mock(self.client.query_orders_for_date, "history")
        calls.attach_mock(self.client.query, "query")
        self.history = [order(status="9")]
        self.publisher.publish("periodic")
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["query", "query", "query", "history", "query"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0].args[1]["status"], "NEW")
        self.current.append(deepcopy(self.current[0]))
        self.publisher.publish("periodic")
        self.assertEqual(len(self.events()), 1)
        self.assertIn("duplicate", self.state.values["last_orders_error"])
        self.assertEqual(self.publisher.published, 2)

    def test_failed_order_ack_retries_identical_sequence_after_restart(self):
        good, failed = Mock(), Mock()
        failed.get.side_effect = TimeoutError("private-kafka-value")
        self.producer.send.side_effect = lambda topic, *args, **kwargs: failed if topic == "order-status" else good
        self.publisher.publish("periodic")
        original = deepcopy(self.events()[0].args[1])
        self.assertEqual(self.publisher.published, 1)
        self.assertEqual(self.publisher.orders_published, 0)
        self.assertIsNone(self.state.values.get("last_holdings_error"))
        self.assertNotIn("private", self.state.values["last_orders_error"])
        self.publisher.close()
        self.producer.send.side_effect = None
        replacement = self.make_publisher()
        self.addCleanup(replacement.close)
        replacement.publish("periodic")
        self.assertEqual(canonical(self.events()[-1].args[1]), canonical(original))
        self.assertIsNone(self.state.values["last_orders_error"])
        self.assertIsNone(self.state.values["last_error"])

    def test_order_failure_does_not_prevent_holdings_and_next_refresh_recovers(self):
        self.client.query_orders_for_date.side_effect = ApiError("History unavailable")
        self.publisher.publish("periodic")
        self.assertEqual(self.publisher.published, 1)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.state.values["last_orders_error"], "History unavailable")
        self.client.query_orders_for_date.side_effect = lambda day: {"code": "000000", "data": []}
        self.publisher.publish("periodic")
        self.assertEqual(self.publisher.published, 2)
        self.assertEqual(len(self.events()), 1)
        self.assertIsNone(self.state.values["last_orders_error"])

    def test_failed_holdings_ack_still_queries_and_publishes_orders(self):
        good, failed = Mock(), Mock()
        failed.get.side_effect = TimeoutError()
        self.producer.send.side_effect = lambda topic, *args, **kwargs: failed if topic == "account-details" else good
        self.publisher.publish("periodic")
        self.assertEqual(self.publisher.published, 0)
        self.assertEqual(len(self.events()), 1)
        self.assertIsNotNone(self.state.values["last_holdings_error"])
        self.assertEqual(self.state.values["last_error"], self.state.values["last_holdings_error"])

    def test_failed_holdings_read_still_queries_orders_but_earlier_failures_do_not(self):
        def response(name):
            if name == "holdings":
                raise ApiError("Holdings unavailable")
            return {"code": "000000", "data": self.current if name == "orders" else {}}
        self.client.query.side_effect = response
        self.publisher.publish("periodic")
        self.assertEqual(self.publisher.published, 0)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.state.values["last_holdings_error"], "Holdings unavailable")
        self.client.query.side_effect = ApiError("Account unavailable")
        self.client.query_orders_for_date.reset_mock()
        self.publisher.publish("periodic")
        self.client.query_orders_for_date.assert_not_called()
        self.assertEqual(len(self.events()), 1)

    def test_whole_failed_batch_retries_after_restart_and_midnight(self):
        self.current = [order("first"), order("second")]
        failed = Mock()
        failed.get.side_effect = TimeoutError()
        self.producer.send.side_effect = lambda topic, *a, **kw: failed if topic == "order-status" else Mock()
        self.publisher.publish("periodic")
        self.assertEqual(len(self.publisher.order_journal.pending()), 2)
        first_attempt = deepcopy(self.events()[0].args[1])
        self.publisher.close()
        self.current, self.history = [], []
        self.producer.send.side_effect = None
        replacement = self.make_publisher()
        replacement.wall = lambda: (NOW + timedelta(days=1)).timestamp()
        self.addCleanup(replacement.close)
        replacement.publish("periodic")
        self.assertEqual(len(self.events()), 3)
        retried = {call.args[1]["order_id"]: call.args[1] for call in self.events()[1:]}
        self.assertEqual(set(retried), {"first", "second"})
        self.assertEqual(canonical(retried["first"]), canonical(first_attempt))
        self.assertEqual(replacement.order_journal.pending(), [])
        self.assertEqual(self.state.values["last_orders_count"], 0)

    def test_mismatched_journal_ack_is_not_reported_as_refresh_success(self):
        journal = Mock()
        journal.prepare.side_effect = lambda snapshot: {**snapshot, "sequence": 1}
        journal.pending.return_value = []
        journal.ack.return_value = False
        with patch("zhuorui.api.order_publication.OrderPublicationJournal", return_value=journal):
            self.publisher.publish("periodic")
        self.assertEqual(self.publisher.orders_published, 1)
        self.assertIn("state changed", self.state.values["last_orders_error"])
        self.assertNotIn("last_orders_publish", self.state.values)

    def test_stale_client_order_success_cannot_clear_recovery_errors(self):
        state = Mock()
        provider = ClientProvider(self.root / "config.json", self.config,
                                  Mock(session_file=self.root / "session.dpapi"), self.settings, state)
        provider.cached = self.client
        self.assertFalse(provider.report_orders_success(object()))
        state.orders_recovered.assert_not_called()
        self.assertTrue(provider.report_orders_success(self.client, last_orders_count=1))
        state.orders_recovered.assert_called_once_with(last_orders_count=1, session_status="valid")
        provider.schedule.observe_logout(NOW.timestamp(), "session_expired")
        self.assertFalse(provider.report_orders_success(self.client))
        self.assertEqual(state.orders_recovered.call_count, 1)

    def test_order_session_expiry_uses_existing_recovery_handler(self):
        api = Mock(session_file=self.root / "session.dpapi")
        provider = ClientProvider(self.root / "config.json", self.config, api,
                                  self.settings, self.state, wall=lambda: NOW.timestamp())
        provider.cached = self.client
        provider.session = {"generation": "synthetic"}
        provider._load_client = Mock(return_value=self.client)
        publisher = self.make_publisher(provider)
        self.addCleanup(publisher.close)
        self.client.query_orders_for_date.side_effect = SessionExpired("Synthetic expiry")
        publisher.publish("periodic")
        self.assertTrue(provider.recovery_needed)
        self.assertEqual(self.state.values["session_status"], "invalid")
        self.assertEqual(publisher.published, 1)
        self.assertEqual(publisher.orders_published, 0)

    def test_midnight_crossing_retries_next_cycle_without_wrong_day_publication(self):
        before = datetime(2026, 9, 30, 3, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=2)
        self.publisher.wall = Mock(side_effect=[before.timestamp(), after.timestamp()])
        self.publisher.publish("periodic")
        self.assertEqual(self.events(), [])
        self.assertIn("Eastern date changed", self.state.values["last_orders_error"])

    def test_journal_path_follows_custom_command_journal_and_supports_override(self):
        self.assertEqual(self.settings.order_snapshot_journal_file,
                         self.root / "commands.order-snapshots.sqlite3")
        self.config["api"]["order_snapshot_journal_file"] = "custom/orders.sqlite3"
        settings = load_listener_settings(self.root / "config.json", self.config)
        self.assertEqual(settings.order_snapshot_journal_file, self.root / "custom/orders.sqlite3")


if __name__ == "__main__":
    unittest.main()
