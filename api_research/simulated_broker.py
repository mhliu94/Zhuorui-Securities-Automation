"""Offline order/holdings model. No network, broker credentials, or UI access.

Prices, liquidity, and balances are synthetic. Models long-only shares, zero
fees, immediate cash accounting, market remainder cancellation, resting limit
remainders, and native FOK all-or-none. These are test assumptions, not verified
Zhuorui market/session rules or wire-enum mappings.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OrderSpec:
    command_id: str
    symbol: str
    side: str
    kind: str
    quantity: int
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class Fill:
    quantity: int
    price: Decimal


@dataclass
class SimOrder:
    order_id: str
    spec: OrderSpec
    status: str
    fills: tuple[Fill, ...] = ()
    reason: str | None = None

    @property
    def filled_quantity(self):
        return sum(fill.quantity for fill in self.fills)

    @property
    def remaining_quantity(self):
        return self.spec.quantity - self.filled_quantity


class SimulatedBroker:
    def __init__(self, cash=Decimal("1000")):
        self.cash = Decimal(cash)
        self.positions = {}
        self.books = {}
        self.orders = {}
        self.submission_count = 0

    def set_book(self, symbol, *, bids=(), asks=()):
        def levels(rows, reverse):
            result = [[Decimal(price), qty] for price, qty in rows]
            if any(not p.is_finite() or p <= 0 or type(q) is not int or q <= 0 for p, q in result):
                raise ValueError("Book levels require positive prices and integer quantities")
            return sorted(result, key=lambda row: row[0], reverse=reverse)
        self.books[symbol] = {"buy": levels(asks, False), "sell": levels(bids, True)}

    def seed_position(self, symbol, quantity, average_cost):
        if type(quantity) is not int or quantity < 0:
            raise ValueError("Invalid starting quantity")
        cost = Decimal(average_cost)
        if not cost.is_finite() or cost < 0:
            raise ValueError("Invalid starting average cost")
        self.positions[symbol] = (quantity, cost)

    @staticmethod
    def _validate(spec):
        if not spec.command_id or not spec.symbol:
            raise ValueError("Command and symbol are required")
        if spec.side not in {"buy", "sell"} or spec.kind not in {"market", "limit", "fok"}:
            raise ValueError("Unknown order side/type")
        if type(spec.quantity) is not int or spec.quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        if spec.kind in {"limit", "fok"}:
            if not isinstance(spec.limit_price, Decimal) or not spec.limit_price.is_finite() or spec.limit_price <= 0:
                raise ValueError("Limit and FOK require a positive Decimal limit price")
        elif spec.limit_price is not None:
            raise ValueError("This simulated market order has no limit price")

    def submit(self, spec, *, lose_acknowledgement=False):
        self._validate(spec)
        # Simulated client IDs permit deterministic timeout/dedup tests. This
        # does NOT establish broker-side idempotency support at Zhuorui.
        if spec.command_id in self.orders:
            existing = self.orders[spec.command_id]
            if existing.spec != spec:
                raise ValueError("Command ID reused with a different order")
            return existing
        self.submission_count += 1
        order = SimOrder(f"SIM-{self.submission_count}", spec, "open")
        self.orders[spec.command_id] = order
        held, avg_cost = self.positions.get(spec.symbol, (0, Decimal("0")))
        if spec.side == "sell" and held < spec.quantity:
            order.status, order.reason = "rejected", "insufficient_shares"
            return order
        levels = self.books.get(spec.symbol, {}).get(spec.side, [])
        remaining, planned = spec.quantity, []
        for index, (price, available) in enumerate(levels):
            if remaining == 0:
                break
            if spec.limit_price is not None:
                if spec.side == "buy" and price > spec.limit_price:
                    break
                if spec.side == "sell" and price < spec.limit_price:
                    break
            quantity = min(remaining, available)
            if quantity:
                planned.append((index, Fill(quantity, price)))
                remaining -= quantity
        if spec.kind == "fok" and remaining:
            order.status, order.reason = "cancelled", "fok_not_fully_executable"
            return order
        value = sum((fill.price * fill.quantity for _, fill in planned), Decimal("0"))
        if spec.side == "buy" and value > self.cash:
            order.status, order.reason = "rejected", "insufficient_cash"
            return order
        quantity = spec.quantity - remaining
        for index, fill in planned:
            levels[index][1] -= fill.quantity
        order.fills = tuple(fill for _, fill in planned)
        if quantity:
            if spec.side == "buy":
                self.cash -= value
                self.positions[spec.symbol] = (held + quantity, (held * avg_cost + value) / (held + quantity))
            else:
                self.cash += value
                self.positions[spec.symbol] = (held - quantity, avg_cost if held > quantity else Decimal("0"))
        if remaining == 0:
            order.status = "filled"
        elif spec.kind == "market":
            order.status, order.reason = "cancelled", "market_remainder_cancelled"
        elif quantity:
            order.status = "partially_filled"
        if lose_acknowledgement:
            raise TimeoutError("Simulated lost acknowledgement after broker processing")
        return order

    def get_order(self, command_id):
        return self.orders.get(command_id)

    def cancel(self, command_id):
        order = self.orders[command_id]
        if order.status in {"open", "partially_filled"}:
            order.status = "cancelled"
        return order

    def holdings(self):
        return {
            "cash": [{"currency": "USD", "amount": format(self.cash, "f")}],
            "securities": [
                {"symbol": symbol, "quantity": str(qty), "average_cost": format(avg, "f")}
                for symbol, (qty, avg) in sorted(self.positions.items()) if qty
            ],
        }
