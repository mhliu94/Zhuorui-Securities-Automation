"""Logout notifications preserve write outcomes and never perform login/replay."""
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from zhuorui.api.commands import CancelCommand
from zhuorui.api.errors import BrokerRejected, LoggedInElsewhere, OrderOutcomeUnknown, SessionError, SessionExpired
from zhuorui.api.execution import CommandExecutor
from zhuorui.api.journal import CommandJournal
from tests.test_api_execution import Clock, RecordingClient, RecordingHoldings, order, trade


class ExecutionSessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.timeline = []
        self.clock = Clock(self.timeline)
        self.client = RecordingClient(self.clock, self.timeline)
        self.provider = Mock(return_value=self.client)
        self.holdings = RecordingHoldings(self.clock, self.timeline)
        self.journal = CommandJournal(Path(self.folder.name) / "commands.sqlite3", {"account": "synthetic"})
        self.events = []
        self.executor = CommandExecutor(
            SimpleNamespace(live_orders_enabled=True), SimpleNamespace(cancel_after_seconds=1),
            self.journal, self.provider, self.holdings,
            lambda command, status, message, **extra: self.events.append((status, extra)),
            now=self.clock.now, wall=self.clock.wall, sleep=self.clock.sleep,
        )

    def tearDown(self):
        self.provider.refresh_from_emulator.assert_not_called()
        self.provider.recover.assert_not_called()
        self.journal.close()
        self.folder.cleanup()

    def test_preflight_logout_reports_the_exact_client_without_submitting(self):
        error = LoggedInElsewhere("Synthetic account logout")
        self.client.query = Mock(side_effect=error)
        self.executor.execute(trade())
        self.provider.report_error.assert_called_once_with(error, client=self.client)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.journal.get("command-1")["state"], "rejected")

    def test_missing_local_session_reports_without_an_acquired_client(self):
        error = SessionError("Synthetic unavailable session")
        self.provider.side_effect = error
        self.executor.execute(trade())
        self.provider.report_error.assert_called_once_with(error, client=None)
        self.assertEqual(self.client.calls, [])

    def test_quote_sizing_logout_reports_without_order_dispatch(self):
        error = SessionExpired("Synthetic quote logout")
        self.client.quantity_for_notional = Mock(side_effect=error)
        command = replace(trade(), quantity=None, notional_usd=Decimal("100"))
        self.executor.execute(command)
        self.provider.report_error.assert_called_once_with(error, client=self.client)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.journal.get(command.command_id)["state"], "rejected")

    def test_submission_logout_stays_rejected_and_is_never_replayed(self):
        error = SessionExpired("Synthetic submission logout")
        self.client.submit_error = error
        command = trade(kind="fok")
        self.executor.execute(command)
        self.executor.execute(command)
        self.provider.report_error.assert_called_once_with(error, client=self.client)
        self.assertEqual([entry[0] for entry in self.client.calls], ["submit"])
        self.assertEqual(self.holdings.requests, ["order_submission"])
        self.assertEqual(self.journal.get(command.command_id)["state"], "rejected")
        self.assertEqual([event[0] for event in self.events], ["rejected", "duplicate"])

    def test_timed_cancel_logout_reports_without_repeating_submission_or_cancel(self):
        error = LoggedInElsewhere("Synthetic cancellation logout")
        self.client.cancel_error = error
        command = trade(kind="fok")
        self.executor.execute(command)
        self.executor.recover_pending_cancellations()
        self.provider.report_error.assert_called_once_with(error, client=self.client)
        self.assertEqual([entry[0] for entry in self.client.calls], ["submit", "cancel"])
        record = self.journal.get(command.command_id)
        self.assertEqual((record["state"], record["cancel_state"]), ("submitted", "rejected"))
        self.assertEqual(self.holdings.requests, ["order_submission", "order_cancellation"])

    def test_partial_cancel_batch_stops_and_reports_logout_without_replay(self):
        error = SessionExpired("Synthetic cancellation logout")
        self.client.orders = [order("ref-first", "2"), order("ref-second", "2"), order("ref-third", "2")]
        self.client.cancel_order = Mock(side_effect=[{"code": "000000"}, error])
        command = CancelCommand("batch", cancel_all=True)
        self.executor.execute(command)
        self.executor.execute(command)
        self.provider.report_error.assert_called_once_with(error, client=self.client)
        self.assertEqual(self.client.cancel_order.call_count, 2)
        self.assertEqual(self.events[0], ("cancel_rejected", {"cancellations_requested": 1}))
        self.assertEqual(self.journal.get(command.command_id)["state"], "rejected")

    def test_pending_cancel_preflight_logout_reports_and_sends_no_write(self):
        error = SessionExpired("Synthetic recovery logout")
        command = trade(kind="fok")
        self.journal.claim(command.command_id, asdict(command))
        self.journal.update(command.command_id, "submitted", reference="existing-ref",
                            cancel_due=self.clock.wall() - 5, cancel_state="pending")
        self.client.query = Mock(side_effect=error)
        self.executor.recover_pending_cancellations()
        self.provider.report_error.assert_called_once_with(error, client=self.client)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.journal.get(command.command_id)["cancel_state"], "pending")

    def test_unknown_write_and_business_rejection_never_trigger_auth_recovery(self):
        for error in (BrokerRejected("Synthetic business reject"), OrderOutcomeUnknown("Synthetic lost acknowledgement")):
            command = trade(type(error).__name__)
            self.client.submit_error = error
            self.executor.execute(command)
        self.provider.report_error.assert_not_called()
        self.assertEqual(self.journal.get("OrderOutcomeUnknown")["state"], "unknown")
        self.assertEqual([entry[0] for entry in self.client.calls], ["submit", "submit"])


if __name__ == "__main__":
    unittest.main()
