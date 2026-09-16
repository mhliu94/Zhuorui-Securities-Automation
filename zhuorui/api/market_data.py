"""Validated US sessions and opposite-side quotes for synthetic market orders."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import re
from zoneinfo import ZoneInfo

from .errors import ApiError

ORDER_BOOK_PATH = "/as_market/api/order/v1/latest"
MARKET_STATUS_PATH = "/as_market/api/market_trade_status/v2/get_market_status"
NEW_YORK = ZoneInfo("America/New_York")
# App 3.1.7: com.zhuorui.quote.model.MarKetState.
SESSION_CODES = {11: "premarket", 4: "regular", 12: "postmarket"}


@dataclass(frozen=True)
class MarketSession:
    name: str
    changed_at: float
    observed_at: float
    valid_until: float

    def validate(self, now):
        if not self.observed_at <= now < self.valid_until:
            raise ApiError("Market status expired or the session changed before dispatch; no order was sent.")


def parse_session(response, *, started, now, max_age):
    rows = response.get("data")
    if not isinstance(rows, list):
        raise ApiError("US market status is unavailable.")
    matches = [row for row in rows if isinstance(row, dict)
               and type(row.get("market")) is int and row["market"] == 2
               and type(row.get("authProductType")) is int and row["authProductType"] == 0]
    if len(matches) != 1:
        raise ApiError("Market status does not identify exactly one US equities session.")
    row = matches[0]
    code, stamp = row.get("statusCode"), row.get("nowDate")
    if type(code) is not int or code not in SESSION_CODES:
        raise ApiError("US market is closed, halted, overnight, or unknown; no Market order was sent.")
    if type(stamp) is not int or not 0 < stamp <= now * 1000:
        raise ApiError("US market status has an invalid transition timestamp.")
    try:
        current = datetime.fromtimestamp(now, NEW_YORK)
        changed = datetime.fromtimestamp(stamp / 1000, NEW_YORK)
    except (ValueError, OverflowError, OSError):
        raise ApiError("US market status has an invalid transition timestamp.") from None
    name = SESSION_CODES[code]
    # Post-market can begin at 13:00 ET on an early-close day; the broker,
    # rather than a weekday-only clock, must positively identify that session.
    start, end = {"premarket": (240, 570), "regular": (570, 960), "postmarket": (780, 1200)}[name]
    minute = current.hour * 60 + current.minute
    transition_minute = changed.hour * 60 + changed.minute
    if (current.weekday() >= 5 or changed.date() != current.date()
            or not start <= transition_minute <= minute < end
            or not 0 <= now - started < max_age):
        raise ApiError("US market status is stale or inconsistent with New York session hours.")
    boundary = current.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0).timestamp()
    # nowDate is the session transition, not the quote or response update time.
    # The status 'delay' flag describes quote entitlement, not status freshness.
    return MarketSession(name, stamp / 1000, now, min(started + max_age, boundary))


def positive_decimal(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 512:
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError()
        parts = number.as_tuple()
        if len(parts.digits) > 128 or abs(parts.exponent) > 100:
            raise ValueError()
        return number
    except (ValueError, ArithmeticError):
        raise ApiError("Order book price, size, or notional amount is invalid.") from None


@dataclass(frozen=True)
class BestQuote:
    price: Decimal
    quantity: Decimal
    timestamp: float
    side: str

    def validate(self, now, max_age):
        if not -5 <= now - self.timestamp <= max_age:
            raise ApiError("Real-time order book is stale or has a future timestamp.")


def parse_best_quote(response, symbol, side, *, now, max_age):
    if side not in ("buy", "sell"):
        raise ApiError("Order side must be buy or sell.")
    row = response.get("data")
    if not isinstance(row, dict) or row.get("code") != symbol or row.get("ts") != "US":
        raise ApiError("Order book does not identify the requested US stock.")
    if row.get("delay", False) is not False:
        raise ApiError("Order book is explicitly marked delayed.")
    stamp = row.get("time")
    if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{17}", stamp):
        raise ApiError("Order book has no valid exchange-local timestamp.")
    try:
        timestamp = datetime.strptime(stamp, "%Y%m%d%H%M%S%f").replace(tzinfo=NEW_YORK).timestamp()
    except (ValueError, OverflowError, OSError):
        raise ApiError("Order book has no valid exchange-local timestamp.") from None
    book_side = "asklist" if side == "buy" else "bidlist"
    levels = row.get(book_side)
    if not isinstance(levels, list) or not levels:
        raise ApiError("Order book has no opposite-side liquidity.")
    parsed = []
    for level in levels:
        if not isinstance(level, dict):
            raise ApiError("Order book contains an invalid level.")
        parsed.append((positive_decimal(level.get("price")), positive_decimal(level.get("qty"))))
    price, quantity = (min if side == "buy" else max)(parsed, key=lambda level: level[0])
    quote = BestQuote(price, quantity, timestamp, "ask" if side == "buy" else "bid")
    quote.validate(now, max_age)
    return quote


def market_limit_price(quote, side):
    if side not in ("buy", "sell"):
        raise ApiError("Order side must be buy or sell.")
    price = positive_decimal(quote.price)
    with localcontext() as context:
        parts = price.as_tuple()
        context.prec = max(28, len(parts.digits) + max(parts.exponent, 0) + 6)
        value = (price * Decimal("1.01" if side == "buy" else "0.99")).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR)
    if value <= 0:
        raise ApiError("Synthetic sell limit rounds down to zero cents.")
    return value


def quantity_at_limit(budget, price):
    budget_num, budget_den = positive_decimal(budget).as_integer_ratio()
    price_num, price_den = positive_decimal(price).as_integer_ratio()
    quantity = (budget_num * price_den) // (budget_den * price_num)
    if quantity <= 0:
        raise ApiError("Notional amount is below one share at the synthetic limit price.")
    return quantity
