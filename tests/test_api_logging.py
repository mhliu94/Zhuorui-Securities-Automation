"""Failure-stage, recovery and broken-log tests without broker or Kafka access."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zhuorui.api.errors import ApiError
from zhuorui.api.listener import log_previous_run
from zhuorui.api.publishing import HoldingsPublisher, ListenerState


class PublicationLoggingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = ListenerState(Path(folder.name) / "state.json", last_error=None)
        self.client, self.producer = Mock(), Mock()
        self.publisher = HoldingsPublisher({}, SimpleNamespace(live_orders_enabled=True, holdings_topic="test"),
                                           self.producer, lambda: self.client, self.state)
        self.enterContext(patch("zhuorui.api.snapshot.account_snapshot", return_value={
            "account_id": "private-account", "positions": [{"symbol": "private-position"}]}))

    def test_broker_failure_stage_is_logged_without_exception_message_then_recovers(self):
        self.client.query.side_effect = ApiError("private-broker-response")
        output = io.StringIO()
        with redirect_stdout(output):
            self.publisher.publish("periodic")
            self.client.query.side_effect = None
            self.publisher.publish("periodic")
        log = output.getvalue()
        self.assertIn('"stage":"account_query"', log)
        self.assertIn('"type":"ApiError"', log)
        self.assertIn('"recovered_after_failures":1', log)
        self.assertNotIn("private-", log)
        self.producer.send.assert_called_once()
        self.assertEqual(self.publisher.published, 1)
        self.assertIsNone(self.state.values["last_error"])

    def test_kafka_ack_failure_is_distinct_from_account_read_failure(self):
        self.producer.send.return_value.get.side_effect = TimeoutError("private-kafka-payload")
        output = io.StringIO()
        with redirect_stdout(output):
            self.publisher.publish("periodic")
        self.assertIn('"stage":"kafka_ack"', output.getvalue())
        self.assertIn('"type":"TimeoutError"', output.getvalue())
        self.assertNotIn("private-", output.getvalue())
        self.assertNotIn("Published account details message.", output.getvalue())
        self.assertEqual(self.publisher.published, 0)

    def test_logging_failure_cannot_turn_confirmed_publication_into_failure(self):
        with patch("builtins.print", side_effect=OSError("full disk")):
            self.publisher.publish("periodic")
        self.producer.send.assert_called_once()
        self.assertEqual(self.publisher.published, 1)
        self.assertIsNone(self.state.values["last_error"])

    def test_worker_reports_unexpected_failure_without_raw_thread_traceback(self):
        output = io.StringIO()
        with patch.object(self.publisher, "publish", side_effect=RuntimeError("private-worker-error")), \
             patch("threading.excepthook") as hook, redirect_stdout(output):
            self.publisher.start()
            self.publisher.thread.join(timeout=3)
        self.assertFalse(self.publisher.thread.is_alive())
        hook.assert_not_called()
        self.assertIn("Publisher worker exited unexpectedly.", output.getvalue())
        self.assertIn('"type":"RuntimeError"', output.getvalue())
        self.assertNotIn("private-worker-error", output.getvalue())

    def test_new_run_reports_missing_shutdown_without_claiming_a_cause(self):
        self.state.path.write_text(json.dumps({"running": True, "pid": 42,
                                              "updated_at": "2026-09-12T07:58:38+00:00",
                                              "last_error": "private-error"}))
        output = io.StringIO()
        with redirect_stdout(output):
            log_previous_run(self.state.path)
        self.assertIn("termination cause is unknown", output.getvalue())
        self.assertIn('"previous_pid":42', output.getvalue())
        self.assertIn("2026-09-12T07:58:38", output.getvalue())
        self.assertNotIn("private-error", output.getvalue())
        self.assertTrue(json.loads(self.state.path.read_text())["running"])
        self.state.path.write_text('{"running":false}')
        with patch("zhuorui.api.listener.log_event") as log:
            log_previous_run(self.state.path)
        log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
