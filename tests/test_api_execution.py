"""Offline execution/deadline tests: no broker, emulator or Kafka calls."""
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest

from zhuorui.api.client import ApiClient
from zhuorui.api.commands import CancelCommand, TradingCommand
from zhuorui.api.errors import ApiError, BrokerRejected, OrderOutcomeUnknown, SessionExpired
from zhuorui.api.execution import CommandExecutor, cancel_references
from zhuorui.api.journal import CommandJournal
from zhuorui.api.publishing import HoldingsPublisher


class Clock:
    def __init__(self, timeline):
        self.value = 100.0
        self.timeline = timeline
        self.sleeps = []

    def now(self):
        return self.value

    def wall(self):
        return 1_800_000_000 + self.value

    def sleep(self, delay):
        self.timeline.append(("sleep", self.value, delay))
        self.sleeps.append(delay)
        self.value += delay


class RecordingClient(ApiClient):
    """Keep real typed order planning while replacing all transport locally."""
    def __init__(self, clock, timeline):
        self.clock, self.timeline = clock, timeline
        self.calls = []
        self.ack_latency = 0.0
        self.submit_error = None
        self.cancel_error = None
        self.response = {"code": "000000", "data": {"orderTxnReference": "synthetic-ref"}}
        self.orders = []
        self.headers = {"userid": "synthetic-user"}
        self.account = {"code": "000000", "data": {"clientId": "synthetic-client"}}
        self.trade_auth = {"code": "000000", "data": {"accountId": "synthetic-client", "userId": "synthetic-user"}}

    def query(self, name):
        self.timeline.append(("query", self.clock.now(), name))
        if name == "account":
            return self.account
        if name == "trade-auth":
            return self.trade_auth
        if name == "orders":
            return {"code": "000000", "data": self.orders}
        return {"code": "000000", "data": {}}

    def _request(self, path, body, *, write=False):
        if not write:
            raise AssertionError("Unexpected untyped read")
        operation = "submit" if path.endswith("entrust_enter") else "cancel"
        self.timeline.append((operation, self.clock.now(), body))
        self.calls.append((operation, self.clock.now(), body))
        if operation == "submit":
            self.clock.value += self.ack_latency
            if self.submit_error is not None:
                raise self.submit_error
            return self.response
        if self.cancel_error is not None:
            raise self.cancel_error
        return {"code": "000000"}


class RecordingHoldings:
    def __init__(self, clock, timeline):
        self.clock, self.timeline = clock, timeline
        self.requests = []

    def request(self, reason):
        self.requests.append(reason)
        self.timeline.append(("holdings_request", self.clock.now(), reason))


def trade(command_id="command-1", kind="market", side="buy"):
    return TradingCommand(command_id, "DEMO", side, 3, kind,
                          Decimal("12.3400") if kind != "market" else None)


def order(reference, state, *, market="US", is_open=True):
    return {"orderTxnReference": reference, "entrustStatus": state,
            "ts": market, "isOpen": is_open}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.timeline = []
        self.clock = Clock(self.timeline)
        self.client = RecordingClient(self.clock, self.timeline)
        self.holdings = RecordingHoldings(self.clock, self.timeline)
        self.journal = CommandJournal(Path(self.temp.name) / "journal.sqlite3", {"account": "synthetic"})
        self.events = []
        self.settings = SimpleNamespace(live_orders_enabled=True)
        self.api_settings = SimpleNamespace(cancel_after_seconds=1)
        self.executor = CommandExecutor(self.settings, self.api_settings, self.journal,
                                        lambda: self.client, self.holdings, self.emit,
                                        now=self.clock.now, wall=self.clock.wall, sleep=self.clock.sleep)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def emit(self, command, status, message, **extra):
        self.events.append((status, extra))
        self.timeline.append(("emit", self.clock.now(), status))

    def test_native_market_both_sides_never_sends_limit_fields(self):
        for side, wire in (("buy", "1"), ("sell", "2")):
            with self.subTest(side=side):
                self.executor.execute(trade(side, side=side))
                operation, _, body = self.client.calls[-1]
                self.assertEqual(operation, "submit")
                self.assertEqual(body["entrustProp"], "MO")
                self.assertEqual(body["entrustBs"], wire)
                self.assertEqual(body["entrustAmount"], 3)
                self.assertNotIn("entrustPrice", body)
                self.assertNotIn("allowPrePost", body)
                self.assertNotIn("timeInForce", body)
        self.assertEqual(self.holdings.requests, ["order_submission"] * 2)
        self.assertEqual(self.clock.sleeps, [])

    def test_limit_preserves_decimal_and_requests_holdings(self):
        self.executor.execute(trade(kind="limit"))
        self.assertEqual(self.client.calls[0][2]["entrustProp"], "LO")
        self.assertEqual(str(self.client.calls[0][2]["entrustPrice"]), "12.3400")
        self.assertEqual(self.holdings.requests, ["order_submission"])
        self.assertEqual(self.events[-1][0], "submitted")

    def test_fok_ack_after_point_two_waits_only_remaining_point_eight(self):
        self.client.ack_latency = 0.2
        self.executor.execute(trade(kind="fok"))
        self.assertEqual(self.client.calls[0][2]["entrustProp"], "LO")
        self.assertNotIn("timeInForce", self.client.calls[0][2])
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 0.8)
        self.assertAlmostEqual(self.client.calls[1][1], 101.0)
        self.assertEqual(self.client.calls[1][2], {"orderTxnReference": "synthetic-ref"})
        names = [event[0] for event in self.timeline]
        self.assertLess(names.index("holdings_request"), names.index("sleep"))
        self.assertLess(names.index("sleep"), names.index("cancel"))
        self.assertEqual(self.holdings.requests, ["order_submission", "order_cancellation"])
        self.assertEqual(self.journal.get("command-1")["cancel_state"], "requested")
        self.assertFalse(self.events[-1][1]["native_fok"])
        self.assertFalse(self.events[-1][1]["cancellation_deadline_missed"])

    def test_fok_late_ack_cancels_immediately_without_another_second(self):
        self.client.ack_latency = 1.4
        self.executor.execute(trade(kind="fok"))
        self.assertEqual(self.clock.sleeps, [])
        self.assertAlmostEqual(self.client.calls[1][1], 101.4)
        self.assertTrue(self.events[-1][1]["cancellation_deadline_missed"])

    def test_lost_ack_requests_holdings_without_resubmit_or_guessing_cancel(self):
        self.client.submit_error = OrderOutcomeUnknown("Synthetic timeout")
        command = trade(kind="fok")
        self.executor.execute(command)
        self.executor.execute(command)
        self.assertEqual([call[0] for call in self.client.calls], ["submit"])
        self.assertEqual(self.holdings.requests, ["order_submission"])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.journal.get(command.command_id)["state"], "unknown")
        self.assertEqual([event[0] for event in self.events], ["unknown", "duplicate"])

    def test_missing_ack_reference_is_unknown_and_never_cancelled(self):
        self.client.response = {"code": "000000", "data": {"orderNo": "not-a-transaction-reference"}}
        self.executor.execute(trade(kind="fok"))
        self.assertEqual([call[0] for call in self.client.calls], ["submit"])
        self.assertEqual(self.journal.get("command-1")["state"], "unknown")
        self.assertEqual(self.holdings.requests, ["order_submission"])

    def test_broker_rejected_attempt_still_requests_holdings(self):
        for error in (BrokerRejected("Synthetic reject"), SessionExpired("Synthetic expiry")):
            self.client.submit_error = error
            command = trade(type(error).__name__, kind="fok")
            self.executor.execute(command)
            self.assertEqual(self.journal.get(command.command_id)["state"], "rejected")
        self.assertEqual(self.holdings.requests, ["order_submission"] * 2)
        self.assertFalse(any(call[0] == "cancel" for call in self.client.calls))

    def test_rejected_and_unknown_submission_queue_holdings_before_status_network_wait(self):
        for error in (BrokerRejected("Synthetic reject"), OrderOutcomeUnknown("Synthetic timeout")):
            with self.subTest(error=type(error).__name__):
                self.timeline.clear()
                self.client.submit_error = error
                command = trade(type(error).__name__, kind="fok")
                self.executor.execute(command)
                names = [entry[0] for entry in self.timeline]
                # Status publication can block on Kafka; it must not postpone
                # the independent holdings request for a dispatched operation.
                self.assertLess(names.index("holdings_request"), names.index("emit"))

    def test_live_gate_disabled_never_opens_a_client_or_writes(self):
        self.settings.live_orders_enabled = False
        self.executor.client_provider = lambda: self.fail("Live-disabled executor opened a client")
        for command in (trade(), CancelCommand("cancel", cancel_all=True)):
            self.executor.execute(command)
            self.assertEqual(self.journal.get(command.command_id)["state"], "disabled")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.holdings.requests, [])

    def test_locked_trade_auth_never_submits_or_requests_submission_publication(self):
        self.client.trade_auth = {"code": "000000", "data": {}}
        self.executor.execute(trade())
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.holdings.requests, [])
        self.assertEqual(self.events[-1][0], "rejected")

    def test_unresolved_submission_blocks_later_command(self):
        self.journal.claim("earlier", {"synthetic": True})
        self.journal.update("earlier", "unknown")
        self.executor.execute(trade())
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.journal.get("command-1")["state"], "blocked")

    def test_cancel_all_uses_states_not_is_open_and_ignores_other_markets(self):
        self.client.orders = [order("rejected", "9"), order("filled", "8"),
                              order("cancelled", "6"), order("part-cancelled", "5"),
                              order("pending-cancel", "3"), order("pending-part-cancel", "4"),
                              order("other-market", "NEW", market="HK"),
                              order("working", "2"), order("partial", "PARTIALLY_FILLED")]
        self.executor.execute(CancelCommand("cancel", cancel_all=True))
        self.assertEqual([call[2]["orderTxnReference"] for call in self.client.calls], ["working", "partial"])
        self.assertEqual(self.holdings.requests, ["order_cancellation"] * 2)

    def test_unknown_state_cannot_partially_execute_cancel_all(self):
        self.client.orders = [order("working", "NEW"), order("unmapped", "FUTURE_STATUS")]
        self.executor.execute(CancelCommand("cancel", cancel_all=True))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.events[-1][0], "rejected")

    def test_specific_cancel_requires_unique_exact_account_reference(self):
        command = CancelCommand("cancel", "wanted")
        for rows in ([], [order("different", "NEW")], [order("wanted", "NEW", market="HK")],
                     [order("wanted", "NEW"), order("wanted", "NEW")]):
            with self.subTest(rows=len(rows)):
                with self.assertRaises(ApiError):
                    cancel_references({"code": "000000", "data": rows}, command)

    def test_cancel_unknown_aborts_additional_cancellations(self):
        self.client.orders = [order("first", "NEW"), order("second", "NEW")]
        self.client.cancel_error = OrderOutcomeUnknown("Synthetic cancellation timeout")
        self.executor.execute(CancelCommand("cancel", cancel_all=True))
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.journal.get("cancel")["state"], "unknown")
        self.assertEqual(self.holdings.requests, ["order_cancellation"])

    def test_recovery_retries_only_unattempted_pending_cancellation(self):
        for state in ("pending", "dispatching", "unknown", "requested", "rejected"):
            command = trade(state, kind="fok")
            self.journal.claim(state, asdict(command))
            self.journal.update(state, "submitted", reference="ref-" + state,
                                cancel_due=self.clock.wall() - 10, cancel_state=state)
        self.executor.recover_pending_cancellations()
        self.executor.recover_pending_cancellations()
        self.assertEqual([call[2] for call in self.client.calls], [{"orderTxnReference": "ref-pending"}])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.journal.get("pending")["cancel_state"], "requested")
        self.assertEqual(self.journal.get("dispatching")["cancel_state"], "dispatching")
        self.assertEqual(self.journal.get("unknown")["cancel_state"], "unknown")

    def test_recovery_waits_until_future_deadline_and_obeys_live_gate(self):
        command = trade(kind="fok")
        self.journal.claim(command.command_id, asdict(command))
        self.journal.update(command.command_id, "submitted", reference="ref-pending",
                            cancel_due=self.clock.wall() + 0.7, cancel_state="pending")
        self.settings.live_orders_enabled = False
        self.executor.recover_pending_cancellations()
        self.assertEqual(self.client.calls, [])
        self.settings.live_orders_enabled = True
        self.executor.recover_pending_cancellations()
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 0.7, places=6)

    def test_slow_holdings_worker_does_not_delay_fok_cancellation_or_coalesce_requests(self):
        entered, release = threading.Event(), threading.Event()
        reasons = []
        publisher = HoldingsPublisher({}, SimpleNamespace(holdings_interval_seconds=30),
                                      None, None, None)

        def blocked_publication(reason):
            reasons.append(reason)
            if reason == "periodic":
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError("Test did not release holdings worker")

        publisher.publish = blocked_publication
        publisher.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            self.executor.holdings = publisher
            self.client.ack_latency = 0.2
            self.executor.execute(trade(kind="fok"))
            self.assertEqual(reasons, ["periodic"])
            self.assertAlmostEqual(self.client.calls[1][1], 101)
        finally:
            release.set()
            publisher.close()
        self.assertEqual(reasons, ["periodic", "order_submission", "order_cancellation"])


class PublishingScheduleTests(unittest.TestCase):
    def test_periodic_every_thirty_seconds_and_immediate_requests_do_not_reset_schedule(self):
        timeline = []
        clock = Clock(timeline)
        publisher = HoldingsPublisher({}, SimpleNamespace(holdings_interval_seconds=30),
                                      None, None, None, now=clock.now)
        publications, waits = [], []
        publisher.publish = lambda reason: publications.append((reason, clock.now()))

        class ScriptedQueue:
            index = 0

            def get(self, timeout):
                waits.append(timeout)
                self.index += 1
                if self.index == 1:
                    raise queue.Empty
                if self.index == 2:
                    clock.value += 5
                    return "order_submission"
                if self.index == 3:
                    clock.value += timeout
                    raise queue.Empty
                return None

        publisher.requests = ScriptedQueue()
        publisher.run()
        self.assertEqual(publications, [("periodic", 100), ("order_submission", 105), ("periodic", 130)])
        self.assertEqual(waits, [0, 30, 25, 30])


if __name__ == "__main__":
    unittest.main()
