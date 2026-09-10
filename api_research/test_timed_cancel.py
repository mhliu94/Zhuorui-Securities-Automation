import unittest
from decimal import Decimal
from unittest.mock import patch

from simulated_broker import OrderSpec, SimulatedBroker
from simulated_timed_cancel import submit_with_timed_cancel


class Clock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []
        self.on_sleep = None

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds
        if self.on_sleep:
            self.on_sleep()


class TimedCancellationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.broker = SimulatedBroker()
        self.spec = OrderSpec("timer-1", "TEST", "buy", "limit", 3, Decimal("10"))
        self.broker.set_book("TEST", asks=[("10", 2), ("11", 1)])

    def run_strategy(self, **kwargs):
        return submit_with_timed_cancel(self.broker, self.spec, now=self.clock.now,
                                       sleep=self.clock.sleep, **kwargs)

    def test_cancel_requested_at_one_second_and_partial_fill_retained(self):
        result = self.run_strategy()
        self.assertEqual(result.cancel_requested_at - result.started_at, 1.0)
        self.assertEqual(result.order.filled_quantity, 2)
        self.assertEqual(result.order.status, "cancelled")
        self.assertEqual(self.broker.positions["TEST"][0], 2)

    def test_late_ack_triggers_cancel_without_another_second_wait(self):
        original = self.broker.submit
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock.value += 1.5
            return result
        with patch.object(self.broker, "submit", side_effect=delayed):
            result = self.run_strategy()
        self.assertEqual(result.cancel_requested_at, 101.5)
        self.assertEqual(self.clock.sleeps, [])

    def test_ack_latency_counts_toward_the_one_second_deadline(self):
        original = self.broker.submit
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock.value += 0.4
            return result
        with patch.object(self.broker, "submit", side_effect=delayed):
            result = self.run_strategy()
        self.assertAlmostEqual(self.clock.sleeps[0], 0.6)
        self.assertEqual(result.cancel_requested_at, 101.0)

    def test_lost_ack_reconciles_without_resubmission(self):
        result = self.run_strategy(lose_acknowledgement=True)
        self.assertTrue(result.acknowledgement_lost)
        self.assertEqual(self.broker.submission_count, 1)
        self.assertEqual(result.order.filled_quantity, 2)
        self.assertEqual(result.order.status, "cancelled")

    def test_unknown_order_identity_causes_no_cancel_or_resubmit(self):
        with patch.object(self.broker, "get_order", return_value=None), patch.object(self.broker, "cancel") as cancel:
            with self.assertRaises(RuntimeError):
                self.run_strategy(lose_acknowledgement=True)
        cancel.assert_not_called()
        self.assertEqual(self.broker.submission_count, 1)

    def test_full_fill_skips_cancel(self):
        self.broker.set_book("TEST", asks=[("10", 3)])
        result = self.run_strategy()
        self.assertIsNone(result.cancel_requested_at)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(result.order.status, "filled")

    def test_terminal_order_before_deadline_skips_cancel(self):
        self.clock.on_sleep = lambda: self.broker.cancel(self.spec.command_id)
        result = self.run_strategy()
        self.assertIsNone(result.cancel_requested_at)
        self.assertEqual(result.order.status, "cancelled")

    def test_native_market_is_not_routed_into_timed_limit_strategy(self):
        self.spec = OrderSpec("market-1", "TEST", "buy", "market", 1)
        with self.assertRaises(ValueError):
            self.run_strategy()
        self.assertEqual(self.broker.submission_count, 0)

    def test_cannot_accept_a_live_transport(self):
        with self.assertRaises(TypeError):
            submit_with_timed_cancel(object(), self.spec, now=self.clock.now, sleep=self.clock.sleep)


if __name__ == "__main__":
    unittest.main()
