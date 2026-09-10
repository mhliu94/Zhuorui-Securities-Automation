"""Synthetic scenarios only. Never uses an actual broker, ADB, or Kafka."""
import unittest
from decimal import Decimal as D
from unittest.mock import Mock, patch

from simulated_broker import OrderSpec, SimulatedBroker
from zhuorui_market_order import ZhuoruiTrader, FILL_OR_KILL_REVOKE_DELAY


def order(kind, qty=3, price=None, *, side="buy", command="test-1"):
    return OrderSpec(command, "TEST", side, kind, qty, D(price) if price is not None else None)


class SimulatedFlowTests(unittest.TestCase):
    def setUp(self):
        self.broker = SimulatedBroker(D("1000"))

    def test_market_buy_sweeps_levels_and_updates_holdings(self):
        self.broker.set_book("TEST", asks=[("10", 1), ("11", 2)])
        result = self.broker.submit(order("market"))
        self.assertEqual(result.status, "filled")
        self.assertEqual([f.price for f in result.fills], [D("10"), D("11")])
        self.assertEqual(self.broker.cash, D("968"))
        self.assertEqual(self.broker.positions["TEST"], (3, D("32") / 3))

    def test_market_sell_updates_cash_and_preserves_remaining_cost(self):
        self.broker.seed_position("TEST", 5, "8")
        self.broker.set_book("TEST", bids=[("10", 1), ("9", 2)])
        self.broker.submit(order("market", side="sell"))
        self.assertEqual(self.broker.cash, D("1028"))
        self.assertEqual(self.broker.positions["TEST"], (2, D("8")))

    def test_market_missing_liquidity_cancels_remainder_in_this_model(self):
        self.broker.set_book("TEST", asks=[("10", 1)])
        result = self.broker.submit(order("market"))
        self.assertEqual((result.status, result.filled_quantity, result.remaining_quantity), ("cancelled", 1, 2))

    def test_limit_cannot_fill_above_buy_limit(self):
        self.broker.set_book("TEST", asks=[("10", 2), ("11", 3)])
        result = self.broker.submit(order("limit", price="10.50"))
        self.assertEqual((result.status, result.filled_quantity), ("partially_filled", 2))
        self.assertEqual(self.broker.cash, D("980"))

    def test_limit_cannot_fill_below_sell_limit(self):
        self.broker.seed_position("TEST", 3, "8")
        self.broker.set_book("TEST", bids=[("10", 1), ("9", 2)])
        result = self.broker.submit(order("limit", price="9.50", side="sell"))
        self.assertEqual(result.filled_quantity, 1)
        self.assertEqual(self.broker.positions["TEST"], (2, D("8")))

    def test_unfilled_limit_remains_open_without_holdings_change(self):
        before = self.broker.holdings()
        self.broker.set_book("TEST", asks=[("11", 3)])
        result = self.broker.submit(order("limit", price="10"))
        self.assertEqual(result.status, "open")
        self.assertEqual(self.broker.holdings(), before)

    def test_native_fok_fills_all_eligible_levels(self):
        self.broker.set_book("TEST", asks=[("10", 1), ("10.50", 2)])
        result = self.broker.submit(order("fok", price="10.50"))
        self.assertEqual((result.status, result.filled_quantity), ("filled", 3))
        self.assertEqual(self.broker.cash, D("969"))

    def test_native_fok_insufficient_depth_has_no_partial_fill_or_book_change(self):
        self.broker.set_book("TEST", asks=[("10", 2), ("11", 5)])
        before = self.broker.holdings()
        result = self.broker.submit(order("fok", price="10"))
        self.assertEqual((result.status, result.filled_quantity), ("cancelled", 0))
        self.assertEqual(self.broker.holdings(), before)
        following = self.broker.submit(order("market", 2, command="after-fok"))
        self.assertEqual(following.fills[0].quantity, 2)
        self.assertEqual(following.fills[0].price, D("10"))

    def test_native_fok_sell_is_all_or_none(self):
        self.broker.seed_position("TEST", 3, "8")
        self.broker.set_book("TEST", bids=[("10", 2), ("9", 3)])
        before = self.broker.holdings()
        result = self.broker.submit(order("fok", price="10", side="sell"))
        self.assertEqual(result.filled_quantity, 0)
        self.assertEqual(self.broker.holdings(), before)

    def test_existing_script_fok_can_leave_partial_fill_after_cancel(self):
        self.broker.set_book("TEST", asks=[("10", 2), ("11", 1)])
        trader = Mock()
        trader.submit_prepared_order.side_effect = lambda **_: self.broker.submit(order("limit", price="10"))
        trader.revoke_visible_order.side_effect = lambda: self.broker.cancel("test-1")
        with patch("zhuorui_market_order.time.sleep") as sleep:
            ZhuoruiTrader.submit_fill_or_kill_order(trader, password=None)
        sleep.assert_called_once_with(FILL_OR_KILL_REVOKE_DELAY)
        self.assertEqual(FILL_OR_KILL_REVOKE_DELAY, 3.0)
        result = self.broker.get_order("test-1")
        self.assertEqual((result.status, result.filled_quantity), ("cancelled", 2))
        self.assertEqual(self.broker.positions["TEST"][0], 2)

    def test_cancel_preserves_fills_and_is_idempotent(self):
        self.broker.set_book("TEST", asks=[("10", 2)])
        self.broker.submit(order("limit", price="10"))
        before = self.broker.holdings()
        self.broker.cancel("test-1")
        self.broker.cancel("test-1")
        self.assertEqual(self.broker.holdings(), before)
        self.assertEqual(self.broker.get_order("test-1").filled_quantity, 2)

    def test_cancel_after_full_fill_does_not_reverse_the_fill(self):
        self.broker.set_book("TEST", asks=[("10", 3)])
        self.broker.submit(order("market"))
        self.assertEqual(self.broker.cancel("test-1").status, "filled")

    def test_lost_ack_is_reconciled_without_second_submission(self):
        self.broker.set_book("TEST", asks=[("10", 6)])
        spec = order("market")
        with self.assertRaises(TimeoutError):
            self.broker.submit(spec, lose_acknowledgement=True)
        observed = self.broker.get_order(spec.command_id)
        self.assertEqual(observed.status, "filled")
        self.assertEqual(self.broker.submission_count, 1)
        self.assertEqual(self.broker.positions["TEST"][0], 3)

    def test_same_command_cannot_double_fill_or_change_parameters(self):
        self.broker.set_book("TEST", asks=[("10", 6)])
        spec = order("market")
        first = self.broker.submit(spec)
        self.assertIs(self.broker.submit(spec), first)
        self.assertEqual(self.broker.positions["TEST"][0], 3)
        with self.assertRaises(ValueError):
            self.broker.submit(order("market", 1))

    def test_rejected_buy_does_not_consume_liquidity_or_cash(self):
        self.broker.set_book("TEST", asks=[("1001", 1)])
        before = self.broker.holdings()
        result = self.broker.submit(order("market", 1))
        self.assertEqual(result.reason, "insufficient_cash")
        self.assertEqual(self.broker.holdings(), before)
        self.assertEqual(self.broker.books["TEST"]["buy"][0][1], 1)

    def test_sell_cannot_create_short_position(self):
        self.broker.set_book("TEST", bids=[("10", 3)])
        result = self.broker.submit(order("market", side="sell"))
        self.assertEqual(result.reason, "insufficient_shares")
        self.assertEqual(self.broker.positions, {})

    def test_holdings_average_cost_after_multiple_buys_and_full_sale(self):
        self.broker.seed_position("TEST", 2, "8")
        self.broker.set_book("TEST", asks=[("10", 2)], bids=[("12", 4)])
        self.broker.submit(order("market", 2))
        self.assertEqual(self.broker.holdings()["securities"], [{"symbol": "TEST", "quantity": "4", "average_cost": "9"}])
        self.broker.submit(order("market", 4, side="sell", command="sale"))
        self.assertEqual(self.broker.holdings()["securities"], [])
        self.assertEqual(self.broker.cash, D("1028"))

    def test_bad_inputs_cannot_change_account(self):
        for spec in [order("market", 0), order("fok", price="NaN"), order("limit", price="-1"), order("unknown")]:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.broker.submit(spec)
        self.assertEqual(self.broker.submission_count, 0)
        self.assertEqual(self.broker.cash, D("1000"))


if __name__ == "__main__":
    unittest.main()
