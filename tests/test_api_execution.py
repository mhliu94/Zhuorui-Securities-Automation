"""Offline execution/deadline tests: no broker, emulator or Kafka calls."""
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock
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

    def prepare_market_order(self, symbol, side, quantity, budget=None):
        if quantity is None:
            quantity = self.quantity_for_notional(symbol, budget)
        return "market", quantity, None, False, {"market_session": "regular"}

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
        self.scheduled = []

    def request(self, reason, *, delay_seconds=0):
        self.requests.append(reason)
        self.scheduled.append((self.clock.now() + delay_seconds, reason))
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
        self.assertEqual(self.clock.sleeps, [5, 5])
        self.assertEqual([entry[1] for entry in self.client.calls], [105, 110])

    def test_mixed_order_burst_waits_five_seconds_for_each_submission(self):
        for kind in ("market", "limit", "fok"):
            self.executor.execute(trade(kind, kind=kind))
        submissions = [call[1] for call in self.client.calls if call[0] == "submit"]
        self.assertEqual(submissions, [105, 110, 115])
        self.assertEqual(self.clock.sleeps, [5, 5, 5, 1])
        self.assertEqual([due for due, reason in self.holdings.scheduled if reason == "order_submission"],
                         [107, 112, 117])
        self.assertEqual(self.client.calls[-1][0:2], ("cancel", 116))

    def test_next_order_delay_starts_after_previous_submission_with_slow_ack(self):
        self.client.ack_latency = 0.4
        self.executor.execute(trade("first", kind="limit"))
        self.executor.execute(trade("second"))
        self.assertAlmostEqual(self.client.calls[0][1], 105)
        self.assertAlmostEqual(self.client.calls[1][1], 110.4)

    def test_delayed_order_uses_session_and_notional_quote_after_wait(self):
        from dataclasses import replace
        command = replace(trade(), quantity=None, notional_usd=Decimal("100"))
        def quantity(symbol, budget):
            self.assertEqual(self.clock.now(), 105)
            return 3
        self.client.quantity_for_notional = quantity
        self.executor.execute(command)
        self.assertEqual(self.timeline[0], ("sleep", 100, 5))
        self.assertEqual(self.client.calls[0][1], 105)

    def test_cancel_commands_and_nonexecuting_orders_have_no_submission_wait(self):
        self.client.orders = [order("working", "2")]
        self.executor.execute(CancelCommand("cancel", cancel_all=True))
        self.settings.live_orders_enabled = False
        self.executor.execute(trade("disabled"))
        self.settings.live_orders_enabled = True
        self.executor.execute(trade("disabled"))  # Duplicate.
        self.journal.claim("uncertain", {"test": True})
        self.journal.update("uncertain", "unknown")
        self.executor.execute(trade("blocked"))
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual([call[0] for call in self.client.calls], ["cancel"])

    def test_failed_delay_does_not_mark_order_as_dispatched_or_queue_holdings(self):
        self.executor.sleep = Mock(side_effect=RuntimeError("synthetic wait failure"))
        self.executor.execute(trade())
        self.assertEqual(self.journal.get("command-1")["state"], "rejected")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.holdings.requests, [])

    def test_limit_uses_cents_and_requests_holdings(self):
        self.executor.execute(trade(kind="limit"))
        self.assertEqual(self.client.calls[0][2]["entrustProp"], "LO")
        self.assertEqual(str(self.client.calls[0][2]["entrustPrice"]), "12.34")
        self.assertEqual(self.client.calls[0][2]["allowPrePost"], "Y")
        self.assertEqual(self.holdings.requests, ["order_submission"])
        self.assertEqual(self.events[-1][0], "submitted")

    def test_fok_ack_after_point_two_waits_only_remaining_point_eight(self):
        self.client.ack_latency = 0.2
        self.executor.execute(trade(kind="fok"))
        self.assertEqual(self.client.calls[0][2]["entrustProp"], "LO")
        self.assertNotIn("timeInForce", self.client.calls[0][2])
        self.assertEqual(len(self.clock.sleeps), 2)
        self.assertEqual(self.clock.sleeps[0], 5)
        self.assertAlmostEqual(self.clock.sleeps[1], 0.8)
        self.assertAlmostEqual(self.client.calls[0][1], 105.0)
        self.assertAlmostEqual(self.client.calls[1][1], 106.0)
        self.assertAlmostEqual(self.journal.get("command-1")["cancel_due"], 1_800_000_106)
        self.assertAlmostEqual(self.holdings.scheduled[0][0], 107.2)
        self.assertEqual(self.client.calls[1][2], {"orderTxnReference": "synthetic-ref"})
        names = [event[0] for event in self.timeline]
        sleeps = [index for index, name in enumerate(names) if name == "sleep"]
        self.assertLess(sleeps[0], names.index("submit"))
        self.assertLess(names.index("holdings_request"), sleeps[1])
        self.assertLess(sleeps[1], names.index("cancel"))
        self.assertEqual(self.holdings.requests, ["order_submission", "order_cancellation"])
        self.assertEqual(self.journal.get("command-1")["cancel_state"], "requested")
        self.assertFalse(self.events[-1][1]["native_fok"])
        self.assertFalse(self.events[-1][1]["cancellation_deadline_missed"])

    def test_fok_late_ack_cancels_immediately_without_another_second(self):
        self.client.ack_latency = 1.4
        self.executor.execute(trade(kind="fok"))
        self.assertEqual(self.clock.sleeps, [5])
        self.assertAlmostEqual(self.client.calls[1][1], 106.4)
        self.assertTrue(self.events[-1][1]["cancellation_deadline_missed"])

    def test_lost_ack_requests_holdings_without_resubmit_or_guessing_cancel(self):
        self.client.submit_error = OrderOutcomeUnknown("Synthetic timeout")
        command = trade(kind="fok")
        self.executor.execute(command)
        self.executor.execute(command)
        self.assertEqual([call[0] for call in self.client.calls], ["submit"])
        self.assertEqual(self.holdings.requests, ["order_submission"])
        self.assertEqual(self.clock.sleeps, [5])
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
            self.assertAlmostEqual(self.client.calls[1][1], 106)
        finally:
            release.set()
            publisher.close()
        self.assertEqual(reasons, ["periodic", "order_cancellation", "order_submission"])


class VirtualQueue(queue.Queue):
    """Queue arrivals and timeouts driven by a clock, without real sleeps."""
    def __init__(self, clock, events):
        super().__init__()
        self.clock, self.events = clock, list(events)

    def get(self, timeout):
        target = self.clock.now() + timeout
        while True:
            try:
                return super().get(block=False)
            except queue.Empty:
                if self.events and self.events[0][0] <= target:
                    arrival, action = self.events.pop(0)
                    self.clock.value = max(self.clock.now(), arrival)
                    action()
                else:
                    self.clock.value = target
                    raise


class PublishingScheduleTests(unittest.TestCase):
    def run_schedule(self, events):
        clock = Clock([])
        publisher = HoldingsPublisher({}, SimpleNamespace(holdings_interval_seconds=30),
                                      None, None, None, now=clock.now)
        publications = []
        def publish(reason):
            publications.append((reason, clock.now()))
        publisher.publish = publish
        actions = []
        for arrival, reason, delay in events:
            action = (lambda: publisher.requests.put(None)) if reason is None else (
                lambda reason=reason, delay=delay: publisher.request(reason, delay_seconds=delay))
            actions.append((arrival, action))
        publisher.requests = VirtualQueue(clock, actions)
        publisher._run()
        return publications

    def test_periodic_every_thirty_seconds_and_immediate_requests_do_not_reset_schedule(self):
        publications = self.run_schedule([(105, "login_recovery", 0), (131, None, 0)])
        self.assertEqual(publications, [("periodic", 100), ("login_recovery", 105), ("periodic", 130)])

    def test_post_submission_refresh_waits_two_seconds_without_shifting_periodic(self):
        publications = self.run_schedule([(105, "order_submission", 2), (131, None, 0)])
        self.assertEqual(publications, [("periodic", 100), ("order_submission", 107), ("periodic", 130)])

    def test_pending_refresh_does_not_delay_periodic_or_immediate_cancellation(self):
        publications = self.run_schedule([(129, "order_submission", 2),
                                          (129.5, "order_cancellation", 0), (132, None, 0)])
        self.assertEqual(publications, [("periodic", 100), ("order_cancellation", 129.5),
                                        ("periodic", 130), ("order_submission", 131)])

    def test_burst_retains_every_request_including_equal_deadlines(self):
        publications = self.run_schedule([(105, "order_submission", 2),
                                          (105, "order_submission", 2),
                                          (106, "order_submission", 2), (109, None, 0)])
        self.assertEqual(publications, [("periodic", 100), ("order_submission", 107),
                                        ("order_submission", 107), ("order_submission", 108)])

    def test_busy_worker_does_not_add_another_delay_when_it_reads_request(self):
        clock = Clock([])
        publisher = HoldingsPublisher({}, SimpleNamespace(holdings_interval_seconds=30),
                                      None, None, None, now=clock.now)
        publications = []
        publisher.publish = lambda reason: publications.append((reason, clock.now()))
        publisher.request("order_submission", delay_seconds=2)
        clock.value = 110  # Worker was occupied past the request's deadline.
        publisher.requests.put(None)
        publisher._run()
        self.assertEqual(publications, [("periodic", 110), ("order_submission", 110)])

    def test_close_drains_delayed_refresh_at_its_deadline(self):
        publications = self.run_schedule([(105, "order_submission", 2), (106, None, 0)])
        self.assertEqual(publications, [("periodic", 100), ("order_submission", 107)])


if __name__ == "__main__":
    unittest.main()
