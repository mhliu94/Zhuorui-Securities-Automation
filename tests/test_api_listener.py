"""Offline Kafka record routing, replay handling, and session cache tests."""
from dataclasses import replace
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zhuorui.api.config import ApiSettings
from zhuorui.api.errors import ApiError, LoggedInElsewhere, OrderOutcomeUnknown, SessionError, SessionExpired
from zhuorui.api.execution import CommandExecutor
from zhuorui.api.journal import CommandJournal
from zhuorui.api.listener import ClientProvider, handle_record, run_listener
from zhuorui.api.listener_config import load_listener_settings


NOW = 2000.0
CONFIG = {"account_id": "synthetic-account", "account_num_id": 7, "server_id": "test-server",
          "kafka": {"bootstrap_servers": "unused.invalid:9092"},
          "api": {"live_orders_enabled": True}}
SETTINGS = load_listener_settings(Path("synthetic-config.json"), CONFIG)
API_SETTINGS = ApiSettings(Path("unused-session"), Path("unused-capture"), Path("unused-apk"))


def record(payload=None, *, offset=10, stamp=1999000, raw=None):
    if payload is None:
        payload = {"id": "producer-id", "account_num_id": 7, "type": "MARKET_ORDER",
                   "symbol": "BILI", "side": "buy", "qty_shares": 1}
    return SimpleNamespace(value=json.dumps(payload).encode() if raw is None else raw,
                           topic="trading-commands", partition=2, offset=offset, timestamp=stamp)


class ApiRecordHandlingTests(unittest.TestCase):
    def setUp(self):
        self.executor, self.emit = Mock(), Mock()

    def handle(self, value):
        with patch("zhuorui.api.listener.time.time", return_value=NOW):
            handle_record(value, CONFIG, SETTINGS, self.executor, self.emit)

    def test_matching_market_dispatches_native_market_command(self):
        self.handle(record())
        self.executor.execute.assert_called_once()
        command = self.executor.execute.call_args.args[0]
        self.assertEqual(command.order_type, "market")
        self.assertIsNone(command.limit_price)
        self.emit.assert_not_called()

    def test_different_account_or_server_has_no_execution_or_result_event(self):
        for updates in ({"account_num_id": 8}, {"server_id": "other-server"}):
            payload = json.loads(record().value)
            payload.update(updates)
            self.handle(record(payload))
        self.executor.execute.assert_not_called()
        self.emit.assert_not_called()

    def test_missing_producer_id_uses_repeatable_kafka_coordinate_identity(self):
        payload = json.loads(record().value)
        payload.pop("id")
        self.handle(record(payload))
        self.handle(record(payload))
        commands = [call.args[0] for call in self.executor.execute.call_args_list]
        self.assertEqual(commands[0].command_id, "kafka:trading-commands:2:10")
        self.assertEqual(commands[0], commands[1])

    def test_stale_missing_future_or_invalid_kafka_timestamp_is_rejected_before_execution(self):
        for stamp in (1000000, None, -1, 0, True, "1999000", 2100000):
            with self.subTest(stamp=stamp):
                self.emit.reset_mock()
                self.handle(record(stamp=stamp))
                self.emit.assert_called_once()
                self.assertEqual(self.emit.call_args.args[1], "rejected")
        self.executor.execute.assert_not_called()

    def test_expired_payload_is_rejected_even_with_fresh_kafka_record(self):
        payload = json.loads(record().value)
        payload["expires_at"] = NOW - 1
        self.handle(record(payload))
        self.executor.execute.assert_not_called()
        self.assertIn("expired", self.emit.call_args.args[2])

    def test_invalid_json_and_missing_account_have_safe_rejection_and_fallback_id(self):
        self.handle(record(raw=b'{"private":"secret",'))
        self.executor.execute.assert_not_called()
        self.assertNotIn("secret", self.emit.call_args.args[2])
        self.assertEqual(self.emit.call_args.kwargs["command_id"], "kafka:trading-commands:2:10")
        payload = json.loads(record().value)
        payload.pop("account_num_id")
        self.handle(record(payload))
        self.executor.execute.assert_not_called()

    def test_parser_error_does_not_call_executor_and_reports_producer_or_kafka_identity(self):
        payload = json.loads(record().value)
        payload["price"] = "25"
        self.handle(record(payload))
        self.executor.execute.assert_not_called()
        self.assertIn("session-aware pricing", self.emit.call_args.args[2])

    def test_executor_validation_error_is_a_rejection(self):
        self.executor.execute.side_effect = ApiError("validation failed")
        self.handle(record())
        self.assertEqual(self.emit.call_args.args[1:3], ("rejected", "validation failed"))


class ApiRecordReplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.journal = CommandJournal(Path(self.directory.name) / "commands.sqlite3", {"account": "synthetic"})
        self.client, self.holdings, self.emit = Mock(), Mock(), Mock()
        self.client.headers = {"userid": "synthetic-user"}
        self.client.query.side_effect = lambda name: {"code": "000000", "data":
            {"clientId": "synthetic-client"} if name == "account" else
            {"accountId": "synthetic-client", "userId": "synthetic-user"}}
        self.client.prepare_market_order.side_effect = lambda symbol, side, quantity, budget: ("market", quantity, None, False, {})
        self.client.submit_order.return_value = {"code": "000000", "data": {"orderTxnReference": "synthetic-ref"}}
        self.executor = CommandExecutor(SETTINGS, API_SETTINGS, self.journal, lambda: self.client,
                                        self.holdings, self.emit, sleep=Mock())

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def handle(self, value):
        with patch("zhuorui.api.listener.time.time", return_value=NOW):
            handle_record(value, CONFIG, SETTINGS, self.executor, self.emit)

    def test_redelivered_same_producer_id_is_not_submitted_twice(self):
        self.handle(record(offset=10))
        self.handle(record(offset=11))
        self.client.submit_order.assert_called_once()
        self.assertEqual(self.emit.call_args.args[1], "duplicate")
        self.assertEqual(self.journal.get("producer-id")["state"], "submitted")

    def test_unknown_submission_pauses_following_commands_and_redelivery(self):
        self.client.submit_order.side_effect = OrderOutcomeUnknown("synthetic timeout")
        self.handle(record())
        self.handle(record())
        payload = json.loads(record().value)
        payload["id"] = "next-producer-id"
        self.handle(record(payload, offset=11))
        self.client.submit_order.assert_called_once()
        self.assertEqual(self.journal.get("producer-id")["state"], "unknown")
        self.assertEqual(self.journal.get("next-producer-id")["state"], "blocked")
        self.assertEqual(self.emit.call_args.args[1], "blocked")
        self.holdings.request.assert_called_once_with("order_submission", delay_seconds=2)

    def test_same_id_with_changed_economics_is_rejected_without_resubmission(self):
        self.handle(record())
        payload = json.loads(record().value)
        payload["qty_shares"] = 2
        self.handle(record(payload, offset=11))
        self.client.submit_order.assert_called_once()
        self.assertEqual(self.emit.call_args.args[1], "rejected")
        self.assertIn("reused", self.emit.call_args.args[2])

    def test_disabled_listener_records_command_without_touching_broker(self):
        self.executor.settings = replace(SETTINGS, live_orders_enabled=False)
        self.handle(record())
        self.client.query.assert_not_called()
        self.client.submit_order.assert_not_called()
        self.assertEqual(self.journal.get("producer-id")["state"], "disabled")


class ApiClientProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.session_file = Path(self.directory.name) / "session.dpapi"
        self.session_file.write_bytes(b"synthetic encrypted session placeholder")
        self.api_settings = replace(API_SETTINGS, session_file=self.session_file)
        self.state = Mock()
        self.provider = ClientProvider(Path("synthetic.json"), CONFIG, self.api_settings, SETTINGS, self.state)

    def tearDown(self):
        self.directory.cleanup()

    def test_unchanged_session_reuses_client_and_changed_file_is_reloaded(self):
        old_client, new_client = object(), object()
        with patch("zhuorui.api.listener.load_session", return_value={"generation": "synthetic"}) as load, \
             patch("zhuorui.api.listener.ApiClient", side_effect=[old_client, new_client]) as create:
            self.assertIs(self.provider(), old_client)
            self.assertIs(self.provider(), old_client)
            stamp = self.session_file.stat().st_mtime_ns
            os.utime(self.session_file, ns=(stamp + 1000000000, stamp + 1000000000))
            self.assertIs(self.provider(), new_client)
            self.assertEqual(load.call_count, 2)
            self.assertEqual(create.call_count, 2)

    def test_missing_or_invalid_replacement_session_never_falls_back_to_cached_client(self):
        with patch("zhuorui.api.listener.load_session", return_value={"generation": "synthetic"}), \
             patch("zhuorui.api.listener.ApiClient", return_value=object()):
            self.provider()
        stamp = self.session_file.stat().st_mtime_ns
        os.utime(self.session_file, ns=(stamp + 1000000000, stamp + 1000000000))
        with patch("zhuorui.api.listener.load_session", side_effect=SessionError("private invalid-session detail")):
            with self.assertRaises(SessionError) as caught:
                self.provider()
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.state.update.call_args.kwargs["session_status"], "unavailable")
        self.session_file.unlink()
        with self.assertRaises(SessionError):
            self.provider()

    def test_auto_import_disabled_performs_no_emulator_access(self):
        with patch("zhuorui.api.emulator.import_emulator_session") as importer:
            self.provider.refresh_from_emulator()
        importer.assert_not_called()

    def test_auto_import_is_throttled_and_invalidates_cached_client(self):
        self.provider.settings = replace(SETTINGS, auto_import_session=True)
        self.provider.cached, self.provider.stamp = object(), 1
        with patch("zhuorui.api.listener.time.monotonic", return_value=100), \
             patch("zhuorui.api.emulator.import_emulator_session") as importer:
            self.provider.refresh_from_emulator()
            self.provider.refresh_from_emulator()
        importer.assert_called_once()
        self.assertIsNone(self.provider.cached)
        self.assertIsNone(self.provider.stamp)

    def test_known_invalid_session_marks_recovery_needed(self):
        self.provider.report_error(SessionExpired("Session invalid"))
        self.assertTrue(self.provider.recovery_needed)
        self.assertTrue(any(call.kwargs.get("session_status") == "invalid" for call in self.state.update.call_args_list))

    def test_displaced_login_blocks_cached_client_and_auto_import_until_manual_replacement(self):
        self.provider.settings = replace(SETTINGS, auto_import_session=True)
        first, second = object(), object()
        with patch("zhuorui.api.listener.load_session", side_effect=[{"generation": "old"}, {"generation": "new"}]), \
             patch("zhuorui.api.listener.ApiClient", side_effect=[first, second]):
            self.assertIs(self.provider(), first)
            self.provider.report_error(LoggedInElsewhere("Login displaced"))
            with self.assertRaises(LoggedInElsewhere):
                self.provider()
            with patch("zhuorui.api.listener.time.monotonic", return_value=100), \
                 patch("zhuorui.api.emulator.import_emulator_session") as importer:
                self.provider.refresh_from_emulator()
            importer.assert_not_called()
            stamp = self.session_file.stat().st_mtime_ns
            os.utime(self.session_file, ns=(stamp + 1000000000, stamp + 1000000000))
            self.assertIs(self.provider(), second)
            self.assertFalse(self.provider.displaced)


class ApiListenerLifecycleTests(unittest.TestCase):
    def setUp(self):
        try:
            from kafka import KafkaConsumer, KafkaProducer
        except ImportError:
            self.skipTest("Install requirements-api.txt for installed Kafka configuration validation")
        self.consumer_defaults = KafkaConsumer.DEFAULT_CONFIG
        self.producer_defaults = KafkaProducer.DEFAULT_CONFIG
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        self.settings = replace(SETTINGS, journal_file=root / "commands.sqlite3",
                                state_file=root / "state.json", stop_file=root / "listener.stop")
        self.producer, self.consumer, self.publisher = Mock(), Mock(), Mock()
        self.publisher.thread.ident = None
        self.publisher.thread.is_alive.return_value = True
        self.clients = Mock()
        self.clients.recovery_needed = False
        self.client = self.clients.return_value
        self.client.headers = {"userid": "synthetic-user"}
        self.client.query.side_effect = lambda name: {"code": "000000", "data":
            {"clientId": "synthetic-client"} if name == "account" else
            {"accountId": "synthetic-client", "userId": "synthetic-user"}}
        self.client.prepare_market_order.side_effect = lambda symbol, side, quantity, budget: ("market", quantity, None, False, {})
        self.client.submit_order.return_value = {"code": "000000", "data": {"orderTxnReference": "synthetic-ref"}}
        self.consumer.poll.return_value = {"unused-partition-key": [record()]}
        self.at_commit = []
        from tests.test_api_execution import Clock
        self.clock = Clock([])

        def committed(_):
            with closing(sqlite3.connect(self.settings.journal_file)) as db:
                state = db.execute("SELECT state FROM commands WHERE id='producer-id'").fetchone()[0]
            self.at_commit.append(state)
            self.settings.stop_file.write_text("stop")
        self.consumer.commit.side_effect = committed

    def tearDown(self):
        if hasattr(self, "directory"):
            self.directory.cleanup()

    def run_offline(self):
        with patch("zhuorui.api.listener.load_settings", return_value=(CONFIG, API_SETTINGS)), \
             patch("zhuorui.api.listener.load_listener_settings", return_value=self.settings), \
             patch("zhuorui.api.listener.ClientProvider", return_value=self.clients), \
             patch("zhuorui.api.listener.HoldingsPublisher", return_value=self.publisher), \
             patch("zhuorui.api.listener.CommandExecutor", side_effect=lambda *a, **kw:
                   CommandExecutor(*a, **kw, now=self.clock.now, wall=self.clock.wall, sleep=self.clock.sleep)), \
             patch("zhuorui.api.listener.time.time", return_value=NOW), \
             patch("builtins.print"), \
             patch("kafka.KafkaProducer", return_value=self.producer) as producer_type, \
             patch("kafka.KafkaConsumer", return_value=self.consumer) as consumer_type:
            try:
                return run_listener(Path(self.directory.name) / "synthetic-config.json")
            finally:
                self.producer_kwargs = producer_type.call_args.kwargs
                self.consumer_kwargs = consumer_type.call_args.kwargs

    def test_two_queued_kafka_orders_submit_after_separate_five_second_delays(self):
        second = json.loads(record().value)
        second["id"] = "second-order"
        self.consumer.poll.side_effect = [{"partition": [record()]},
                                         {"partition": [record(second, offset=11)]}]
        submitted = []
        response = self.client.submit_order.return_value
        def submit(*args, **kwargs):
            submitted.append(self.clock.now())
            return response
        self.client.submit_order.side_effect = submit
        def committed(_):
            if len(submitted) == 2:
                self.settings.stop_file.write_text("stop")
        self.consumer.commit.side_effect = committed
        self.assertEqual(self.run_offline(), 0)
        self.assertEqual(submitted, [105, 110])
        self.assertEqual(self.clock.sleeps, [5, 5])
        self.assertEqual(self.consumer.commit.call_count, 2)

    def test_installed_kafka_options_commit_after_durable_submission_and_clean_stop(self):
        self.assertEqual(self.run_offline(), 0)
        self.assertEqual(set(self.producer_kwargs) - set(self.producer_defaults), set())
        self.assertEqual(set(self.consumer_kwargs) - set(self.consumer_defaults), set())
        self.assertGreater(self.consumer_kwargs["request_timeout_ms"], self.consumer_kwargs["session_timeout_ms"])
        self.assertFalse(self.consumer_kwargs["enable_auto_commit"])
        self.assertEqual(self.consumer_kwargs["max_poll_records"], 1)
        self.assertEqual(self.at_commit, ["submitted"])
        self.client.submit_order.assert_called_once()
        self.consumer.close.assert_called_once_with(autocommit=False, timeout_ms=10000)
        self.publisher.start.assert_called_once()
        self.publisher.close.assert_called_once()
        self.producer.flush.assert_called_once()
        self.producer.close.assert_called_once()
        state = json.loads(self.settings.state_file.read_text())
        self.assertFalse(state["running"])
        self.assertFalse(state["kafka_connected"])

    def test_commit_failure_leaves_submission_durable_and_still_cleans_up(self):
        self.consumer.commit.side_effect = RuntimeError("synthetic commit failure")
        with patch("zhuorui.api.listener.log_event") as log, self.assertRaises(ApiError):
            self.run_offline()
        self.assertTrue(any(call.kwargs.get("stage") == "kafka_commit" for call in log.call_args_list))
        with closing(sqlite3.connect(self.settings.journal_file)) as db:
            state = db.execute("SELECT state FROM commands WHERE id='producer-id'").fetchone()[0]
        self.assertEqual(state, "submitted")
        self.client.submit_order.assert_called_once()
        self.publisher.close.assert_called_once()
        self.producer.close.assert_called_once()
        self.assertFalse(json.loads(self.settings.state_file.read_text())["running"])

    def test_session_setup_failure_records_stage_and_closes_journal(self):
        opened = []
        def open_journal(*args):
            journal = CommandJournal(*args)
            opened.append(journal)
            return journal
        with patch("zhuorui.api.listener.ClientProvider", side_effect=SessionError("invalid saved state")), \
             patch("zhuorui.api.listener.load_settings", return_value=(CONFIG, API_SETTINGS)), \
             patch("zhuorui.api.listener.load_listener_settings", return_value=self.settings), \
             patch("zhuorui.api.listener.log_event") as log, \
             patch("zhuorui.api.listener.CommandJournal", side_effect=open_journal):
            with self.assertRaises(ApiError):
                run_listener(Path(self.directory.name) / "synthetic-config.json")
        self.assertTrue(any(call.kwargs.get("stage") == "session_provider" for call in log.call_args_list))
        state = json.loads(self.settings.state_file.read_text())
        self.assertFalse(state["running"])
        self.assertEqual(state["shutdown_reason"], "runtime_error")
        self.assertIn("stopped_at", state)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].db.execute("SELECT 1")

    def test_consumer_close_failure_does_not_skip_other_cleanup(self):
        self.consumer.close.side_effect = RuntimeError("synthetic close failure")
        self.assertEqual(self.run_offline(), 0)
        self.publisher.close.assert_called_once()
        self.producer.flush.assert_called_once()
        self.producer.close.assert_called_once()
        state = json.loads(self.settings.state_file.read_text())
        self.assertIn("consumer", state["shutdown_errors"])
        self.assertFalse(state["running"])


if __name__ == "__main__":
    unittest.main()
