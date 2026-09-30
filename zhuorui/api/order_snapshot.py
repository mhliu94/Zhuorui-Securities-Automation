"""Convert broker order reads to KTrader's complete order-status v1 snapshots.

This module never reads commands or guesses executions from placement success.
The app 3.1.7 ``OrderInfoResponse.OrderInfoModel`` supplies ``orderId()`` from
``orderTxnReference``, ``orderTime()`` from ``entrustTime`` (Unix milliseconds),
``orderQty()`` from ``entrustAmount`` and ``dealQty()`` from ``businessAmount``.
Sequence numbers are assigned by the durable publisher, not by this mapper.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import math
import re
from zoneinfo import ZoneInfo

from .errors import ApiError
from .signing import canonical
from .snapshot import snapshot_identity

NEW_YORK = ZoneInfo("America/New_York")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_MESSAGE_BYTES = 65536

# App 3.1.7: transaction.enums.OrderState.HS.<clinit> and RW.<clinit>.
# NoRegister / WaitForRegister / PendingReview precede acceptance;
# HostRegister is accepted. PartCancelled may still contain executed shares.
# KTrader has no pending-replace, suspended or done-for-day state; leave those
# UNKNOWN rather than claiming an order expired or its modification succeeded.
STATUSES = {
    "0": "PENDING_NEW", "1": "PENDING_NEW", "H": "PENDING_NEW",
    "2": "NEW", "3": "PENDING_CANCEL", "4": "PENDING_CANCEL",
    "7": "PARTIALLY_FILLED", "E": "PARTIALLY_FILLED", "8": "FILLED",
    "5": "CANCELED", "6": "CANCELED", "F": "EXPIRED", "G": "EXPIRED",
    "9": "REJECTED", "J": "REJECTED",
    "ACK": "PENDING_NEW", "PENDING_NEW": "PENDING_NEW", "NEW": "NEW",
    "REPLACED": "NEW", "PENDING_CANCEL": "PENDING_CANCEL",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED",
    "CANCELED": "CANCELED", "REJECTED": "REJECTED",
}

# App OrderType.Companion.orderType2Text / needStopPrice / isTrailingType.
# Market-if-touched and limit-if-touched remain OTHER: a touched threshold is
# not a stop trigger. A trailing limit is still a trailing order in v1.
ORDER_TYPES = {
    "MO": "MARKET", "MARKET": "MARKET", "AO": "MARKET",
    "LO": "LIMIT", "LIMIT": "LIMIT", "ELO": "LIMIT",
    "ALO": "LIMIT", "SLO": "LIMIT", "STP": "STOP",
    "STL": "STOP_LIMIT", "STOP_LIMIT": "STOP_LIMIT",
    "TS": "TRAILING_STOP", "TSL": "TRAILING_STOP",
}


class OrderSnapshotError(ApiError):
    """Broker orders cannot be safely represented in the consumer contract."""


def _number(value, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise OrderSnapshotError(f"Invalid numeric {field} in order data.")
    try:
        wire = str(value)
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", wire):
            raise InvalidOperation
        result = Decimal(wire)
    except (InvalidOperation, ValueError):
        raise OrderSnapshotError(f"Invalid numeric {field} in order data.") from None
    if not result.is_finite():
        raise OrderSnapshotError(f"Nonfinite {field} in order data.")
    # The UI decodes JSON numbers as binary floats. Reject finite Decimal values
    # which would become infinity there, and bound work/serialized numeric size.
    parts = result.as_tuple()
    represented = float(result)
    if (len(parts.digits) > 128 or abs(parts.exponent) > 308
            or not math.isfinite(represented) or (result != 0 and represented == 0)):
        raise OrderSnapshotError(f"Unsupported numeric precision for {field} in order data.")
    return result


def _text(value, field: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise OrderSnapshotError(f"Missing or invalid {field} in order data.")
    return value


def _creation_time(value) -> datetime:
    milliseconds = _number(value, "entrustTime")
    if milliseconds <= 0 or milliseconds != milliseconds.to_integral_value():
        raise OrderSnapshotError("Order creation time must be a positive Unix millisecond timestamp.")
    try:
        return EPOCH + timedelta(milliseconds=int(milliseconds))
    except (OverflowError, ValueError):
        raise OrderSnapshotError("Order creation time is outside the supported timestamp range.") from None


def _price(row: dict, field: str, *, required: bool = False) -> Decimal | None:
    value = row.get(field)
    if value is None:
        if required:
            raise OrderSnapshotError(f"Order is missing required {field}.")
        return None
    price = _number(value, field)
    if price < 0:
        raise OrderSnapshotError(f"Order {field} must be nonnegative.")
    return price


def _average_fill(row: dict, filled: Decimal) -> Decimal | None:
    # App TradeIncomeShareDialog.calculateIncomeData computes dealPrice from
    # dealList businessBalance / businessAmount and uses costPrice separately as
    # the profit baseline. Therefore costPrice is NOT average execution price.
    # Weight per-unit businessPrice by filled units to avoid assumptions about
    # contract multipliers or broker rounding of businessBalance.
    rows = row.get("bargainList")
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise OrderSnapshotError("Unexpected order execution list shape.")
    total, value = Decimal(0), Decimal(0)
    with localcontext() as context:
        context.prec = 280
        for item in rows:
            if not isinstance(item, dict):
                raise OrderSnapshotError("Invalid order execution record.")
            amount = _number(item.get("businessAmount"), "execution businessAmount")
            price = _number(item.get("businessPrice"), "execution businessPrice")
            if amount <= 0 or price < 0:
                raise OrderSnapshotError("Order executions require positive quantities and nonnegative prices.")
            total += amount
            value += amount * price
        if total > filled:
            raise OrderSnapshotError("Execution list exceeds cumulative filled quantity.")
        if filled == 0 or total != filled:
            return None
        with localcontext() as average_context:
            average_context.prec = 128
            return _number(value / total, "average_fill_price")


def _message(row: dict) -> str | None:
    source = row.get("message")
    # The broker model's rejectReason() returns a localized LinkedHashMap.
    # Only English text is selected; do not serialize an arbitrary broker map.
    if isinstance(source, dict):
        source = next((source[key] for key in ("en_US", "en-US", "en", "en_us")
                       if isinstance(source.get(key), str) and source[key]), None)
    if source is None or source == "":
        source = row.get("opRemark") or row.get("remark")
    if source is None or source == "":
        return None
    if not isinstance(source, str):
        raise OrderSnapshotError("Invalid order message text.")
    return "".join(c for c in source if ord(c) >= 32 and ord(c) != 127
                   and not 0xD800 <= ord(c) <= 0xDFFF)[:2000] or None


def order_snapshots(config: dict, response: dict, *, now: datetime | None = None) -> list[dict]:
    """Map all orders created today in US Eastern, including terminal orders.

    ``now`` is the aware source observation time and supplies ``updated_at``:
    the broker's order model has no last-update timestamp. Missing/null ``data``
    is the broker's empty-success encoding, never a failed-read substitute.
    Amounts stay Decimal until the publisher renders plain JSON numbers.
    """
    observed = datetime.now(timezone.utc) if now is None else now
    if not isinstance(observed, datetime) or observed.utcoffset() is None:
        raise OrderSnapshotError("Order observation time must be timezone-aware.")
    observed = observed.astimezone(timezone.utc)
    today = observed.astimezone(NEW_YORK).date()
    if not isinstance(response, dict) or response.get("code") != "000000":
        raise OrderSnapshotError("A successful orders response is required.")
    rows = response.get("data")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise OrderSnapshotError("Unexpected orders response shape.")
    if not rows:
        return []
    account_id, account_num_id, _ = snapshot_identity(config)
    account_id = _text(account_id, "account_id", 128)
    snapshots, ids = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise OrderSnapshotError("Invalid order record.")
        created = _creation_time(row.get("entrustTime"))
        if created.astimezone(NEW_YORK).date() != today:
            continue
        if created > observed:
            raise OrderSnapshotError("Order creation time is after its source observation.")
        # orderNo has separate semantics in the APK. Falling back to it can
        # change the Kafka partition key when a transaction reference arrives.
        order_id = _text(row.get("orderTxnReference"), "orderTxnReference", 256)
        if order_id in ids:
            raise OrderSnapshotError("Today's orders contain duplicate transaction references.")
        ids.add(order_id)
        symbol = _text(_text(row.get("code"), "code", 64).upper(), "code", 64)
        if re.search(r"\s", symbol):
            raise OrderSnapshotError("Invalid order symbol whitespace.")
        side = row.get("entrustBs")
        if side not in ("1", "2"):
            raise OrderSnapshotError("Unrecognized order side.")
        kind = _text(row.get("entrustProp"), "entrustProp", 32)
        state = row.get("entrustStatus")
        if state is not None:
            state = _text(state, "entrustStatus", 64)
        quantity = _number(row.get("entrustAmount"), "entrustAmount")
        filled = _number(row.get("businessAmount"), "businessAmount")
        if quantity <= 0 or not 0 <= filled <= quantity:
            raise OrderSnapshotError("Order quantity or cumulative filled quantity is invalid.")
        status = STATUSES.get(state, "UNKNOWN")
        # REPLACED/HostRegister can report already executed units. Use the
        # broker's cumulative fill, never a command or placement acknowledgement.
        if status == "NEW" and 0 < filled < quantity:
            status = "PARTIALLY_FILLED"
        if (status == "NEW" and filled == quantity) or (status == "PENDING_NEW" and filled > 0):
            raise OrderSnapshotError("Broker order state conflicts with its cumulative executions.")
        if status == "FILLED" and filled != quantity:
            raise OrderSnapshotError("FILLED order must report the entire quantity executed.")
        if status == "PARTIALLY_FILLED" and not 0 < filled < quantity:
            raise OrderSnapshotError("PARTIALLY_FILLED order must report a positive incomplete fill.")
        if state in {"4", "5", "G"} and not 0 < filled < quantity:
            raise OrderSnapshotError("Broker partial execution state requires a positive incomplete fill.")
        result = {
            "schema_version": 1, "account_id": account_id,
            "account_num_id": account_num_id, "order_id": order_id,
            "created_at": created.isoformat(), "updated_at": observed.isoformat(),
            "symbol": symbol, "side": "BUY" if side == "1" else "SELL",
            "order_type": ORDER_TYPES.get(kind, "OTHER"), "status": status,
            "quantity": quantity, "filled_quantity": filled,
            "average_fill_price": _average_fill(row, filled),
        }
        limit = _price(row, "entrustPrice", required=result["order_type"] in {"LIMIT", "STOP_LIMIT"})
        stop = _price(row, "stopPrice", required=result["order_type"] in {"STOP", "STOP_LIMIT"})
        if limit is not None and (result["order_type"] in {"LIMIT", "STOP_LIMIT"}
                                  or (kind in {"LIT", "TSL", "ODD"} and limit > 0)):
            result["limit_price"] = limit
        if stop is not None and (result["order_type"] in {"STOP", "STOP_LIMIT"} or stop > 0):
            result["stop_price"] = stop
        market = row.get("ts")
        if market is not None:
            market = _text(market, "ts", 32)
        currency = {"US": "USD", "HK": "HKD", "SH": "CNY", "SZ": "CNY"}.get(market)
        if currency is not None:
            result["currency"] = currency
        if row.get("timeInForce") is not None:
            result["time_in_force"] = _text(row["timeInForce"], "timeInForce", 32)
        message = _message(row)
        notes = []
        if status == "UNKNOWN":
            # Status is a broker enum, not arbitrary free text or identifiers.
            label = state if state is not None and re.fullmatch(r"[A-Za-z0-9_]{1,64}", state) else "unavailable"
            notes.append(f"Unmapped broker order status: {label}.")
        if kind not in ORDER_TYPES:
            label = kind if re.fullmatch(r"[A-Za-z0-9_]{1,32}", kind) else "unavailable"
            notes.append(f"Broker order type: {label}.")
        if state == "E":
            notes.append("Broker order awaits modification.")
        if message:
            notes.append(message)
        if notes:
            result["message"] = " ".join(notes)[:2000]
        # Reserve the maximum sequence representation now. The publisher also
        # checks the actual serialized event before sending it to Kafka.
        if len(canonical({**result, "sequence": 9007199254740991})) > MAX_MESSAGE_BYTES:
            raise OrderSnapshotError("Order snapshot exceeds 64 KiB.")
        snapshots.append(result)
    return snapshots


def order_detail_snapshot(config: dict, response: dict, listed: dict, *, now: datetime) -> dict:
    """Validate a fresh detail read before using its cumulative executions.

    List/history responses omit ``bargainList``. The app's v2 detail endpoint
    supplies the same order model with that execution breakdown. Require the
    immutable identity to match the list read, while allowing fills, status and
    order terms to advance between reads.
    """
    if (not isinstance(response, dict) or response.get("code") != "000000"
            or not isinstance(response.get("data"), dict)):
        raise OrderSnapshotError("Order detail response is missing an order record.")
    snapshots = order_snapshots(config, {"code": "000000", "data": [response["data"]]}, now=now)
    if len(snapshots) != 1:
        raise OrderSnapshotError("Order detail does not identify today's requested order.")
    detailed = snapshots[0]
    for field in ("account_id", "account_num_id", "order_id", "created_at", "symbol", "side"):
        if detailed[field] != listed[field]:
            raise OrderSnapshotError("Order detail identity does not match its requested order.")
    if detailed["filled_quantity"] < listed["filled_quantity"]:
        raise OrderSnapshotError("Order detail has stale cumulative executions; the next refresh will retry.")
    terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
    if listed["status"] in terminal and detailed["status"] not in terminal:
        raise OrderSnapshotError("Order detail has stale order status; the next refresh will retry.")
    if detailed["filled_quantity"] > 0 and detailed["average_fill_price"] is None:
        raise OrderSnapshotError("Order detail is missing complete execution prices; the next refresh will retry.")
    # Some detail versions omit optional listing metadata. All required order
    # fields above come from the detail response itself, never from placement.
    for field in ("currency", "time_in_force"):
        if field not in detailed and field in listed:
            detailed[field] = listed[field]
    return detailed
