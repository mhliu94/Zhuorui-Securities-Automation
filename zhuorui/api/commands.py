"""Pure validation of the existing KTrader Kafka command format.

No Android, broker, Kafka, or UI modules are imported here.  A command without a
producer ID must be assigned its stable Kafka topic/partition/offset identity by
the listener; an execution-time ID would make a redelivery a second order.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

from zhuorui.common.config import configured_account_id, configured_account_num_id
from .errors import ApiError


class CommandError(ApiError):
    """A malformed or unsupported command, safe to report without its payload."""


@dataclass(frozen=True)
class TradingCommand:
    command_id: str
    symbol: str
    side: str
    quantity: int | None
    order_type: str
    limit_price: Decimal | None
    notional_usd: Decimal | None = None
    allow_pre_post: bool = False


@dataclass(frozen=True)
class CancelCommand:
    command_id: str
    order_reference: str | None = None
    cancel_all: bool = False


ORDER_TYPES = {
    "market": "market", "market_order": "market",
    "limit": "limit", "limit_order": "limit",
    "fok": "fok", "fill_or_kill": "fok", "limit_order_fok": "fok",
    "timed_cancel": "fok",
}
CANCEL_TYPES = {
    "cancel", "cancel_all", "cancel_order", "cancel_orders",
    "cancel_open_order", "cancel_open_orders", "cancel_pending_order",
    "cancel_pending_orders", "cancel_all_orders", "cancel_all_open_orders",
    "cancel_all_pending_order", "cancel_all_pending_orders",
}
TYPE_KEYS = ("order_type", "orderType", "type")
ACTION_KEYS = ("action", "cmd", "command", "command_type", "commandType",
               "event", "event_type", "eventType", "request_type", "requestType")
ID_KEYS = ("id", "command_id", "commandId", "order_id", "orderId")
ACCOUNT_KEYS = ("account_id", "accountId", "account")
ACCOUNT_NUM_KEYS = ("account_num_id", "accountNumId", "account_numeric_id",
                    "accountNumericId", "account_num", "accountNum")
SERVER_KEYS = ("server_id", "serverId", "target_server_id", "targetServerId")


def decode_command(value: bytes | str) -> dict:
    """Decode without float rounding, nonfinite numbers, or duplicate JSON keys."""
    if not isinstance(value, (bytes, str)) or len(value) > 65536:
        raise CommandError("Kafka command must be a JSON object of at most 64 KiB.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise CommandError("Kafka command has duplicate JSON keys.")
            result[key] = item
        return result

    def nonfinite(_):
        raise CommandError("Kafka command numbers must be finite.")

    try:
        decoded = json.loads(value, parse_float=Decimal, parse_constant=nonfinite,
                             object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CommandError("Kafka command must be valid UTF-8 JSON.") from None
    if not isinstance(decoded, dict):
        raise CommandError("Kafka command must be a JSON object.")
    return decoded


def _present(payload, keys):
    return [(key, payload[key]) for key in keys
            if key in payload and payload[key] is not None and payload[key] != ""]


def _text(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CommandError(f"{label} must be text or an integer.")
    result = str(value).strip()
    if not result or len(result) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in result):
        raise CommandError(f"{label} must contain 1-256 printable characters.")
    return result


def _normalize(value):
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()


def _alias(payload, keys, parser, default=None):
    values = [parser(value) for _, value in _present(payload, keys)]
    if not values:
        return default
    if any(value != values[0] for value in values[1:]):
        raise CommandError(f"Conflicting aliases for {keys[0]}.")
    return values[0]


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CommandError("Quantity must be a positive whole number of shares.")
    text = str(value).strip()
    if len(text) > 64 or not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", text):
        raise CommandError("Quantity must be a positive whole number of shares.")
    result = int(text.replace(",", ""))
    if result <= 0:
        raise CommandError("Quantity must be a positive whole number of shares.")
    return result


def _positive_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CommandError("Price and notional must be finite positive decimals.")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise CommandError("Price and notional must be finite positive decimals.") from None
    if not result.is_finite() or result <= 0:
        raise CommandError("Price and notional must be finite positive decimals.")
    parts = result.as_tuple()
    if len(parts.digits) > 128 or abs(parts.exponent) > 100:
        raise CommandError("Price and notional exceed supported numeric precision.")
    return result


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "y", "n"}:
        return value.strip().lower() in {"true", "y"}
    raise CommandError("allow_pre_post must be a boolean or Y/N.")


def command_targets_configured_account(payload: dict, config: dict, *, server_id=None) -> bool:
    """Require account routing and check every provided account/server alias.

    Local numeric account IDs and names remain interchangeable in account_id,
    as in the UI listener.  A server selector is optional, but must match when
    present.  A mismatching selector means this record belongs to another worker.
    """
    if not isinstance(payload, dict):
        raise CommandError("Kafka command must be a JSON object.")
    account = configured_account_id(config)
    account_num = configured_account_num_id(config)
    if not account or account_num is None or isinstance(account_num, bool) or account_num <= 0:
        raise CommandError("Configure account_id and positive account_num_id before consuming commands.")
    account_values = _present(payload, ACCOUNT_KEYS)
    numeric_values = _present(payload, ACCOUNT_NUM_KEYS)
    if not account_values and not numeric_values:
        raise CommandError("Kafka commands require an explicit account_id or account_num_id.")
    for _, value in account_values:
        if _text(value, "Account selector") not in {str(account), str(account_num)}:
            return False
    for _, value in numeric_values:
        text = _text(value, "Numeric account selector")
        if not re.fullmatch(r"[0-9]+", text):
            raise CommandError("Numeric account selector must be a positive integer.")
        if int(text) != account_num:
            return False
    kafka = config.get("kafka", {})
    if not isinstance(kafka, dict):
        raise CommandError("kafka configuration must be an object.")
    target_server = server_id or kafka.get("server_id") or config.get("server_id")
    for _, value in _present(payload, SERVER_KEYS):
        if not target_server:
            raise CommandError("Configure server_id to validate commands addressed to a server.")
        if _text(value, "Server selector") != str(target_server):
            return False
    return True


def parse_command(payload: dict, config: dict, *, message_id=None, server_id=None):
    """Return a validated command, or None for an explicitly different target.

    FOK means the user's one-second cancellation policy, not native FOK.  Market
    price aliases are rejected rather than changing the order's execution type.
    Legacy cancellation without a reference is represented as cancel_all; the
    transport must independently decide whether it can safely support that flow.
    """
    if not command_targets_configured_account(payload, config, server_id=server_id):
        return None
    command_id = _alias(payload, ID_KEYS, lambda v: _text(v, "Command ID"))
    if command_id is None:
        if message_id is None:
            raise CommandError("Command requires an ID or its stable Kafka message identity.")
        command_id = _text(message_id, "Kafka message identity")

    type_values = [_normalize(_text(value, "Command type"))
                   for _, value in _present(payload, TYPE_KEYS + ACTION_KEYS)]
    cancellations = [value for value in type_values if value in CANCEL_TYPES]
    if cancellations:
        if any(value in ORDER_TYPES for value in type_values):
            raise CommandError("Command cannot request both submission and cancellation.")
        reference = _alias(payload, ("orderTxnReference", "order_txn_reference", "order_reference",
                                    "orderReference", "reference"),
                           lambda v: _text(v, "Order transaction reference"))
        explicit_all = any("all" in value.split("_") for value in cancellations)
        if reference and explicit_all:
            raise CommandError("Cancel-all commands cannot include one order reference.")
        return CancelCommand(command_id, reference, reference is None)

    actions = [_normalize(_text(value, "Command action"))
               for _, value in _present(payload, ACTION_KEYS)]
    if any(value not in {"order", "place_order", "submit_order", "trade"} for value in actions):
        raise CommandError("Unsupported command action.")

    def order_type(value):
        normalized = ORDER_TYPES.get(_normalize(_text(value, "Order type")))
        if normalized is None:
            raise CommandError("Supported order types are MARKET_ORDER, LIMIT_ORDER and LIMIT_ORDER_FOK.")
        return normalized

    kind = _alias(payload, TYPE_KEYS, order_type, "market")
    tif = _alias(payload, ("time_in_force", "tif", "timeInForce"),
                 lambda v: _normalize(_text(v, "Time in force")), "day")
    if tif in {"fok", "fill_or_kill"}:
        if kind == "market":
            raise CommandError("Market orders cannot use the limit-order timed cancellation policy.")
        kind = "fok"
    elif tif != "day":
        raise CommandError("Only DAY or the limit-order FOK cancellation policy is supported.")
    symbol = _alias(payload, ("symbol", "ticker", "code"), lambda v: _text(v, "Symbol").upper())
    if symbol is None or not re.fullmatch(r"[A-Z0-9.=\-]{1,16}", symbol):
        raise CommandError("Symbol must contain 1-16 letters, digits, dots, dashes or equals signs.")
    side = _alias(payload, ("side", "direction"), lambda v: _text(v, "Side").lower())
    if side not in {"buy", "sell"}:
        raise CommandError("Command side must be buy or sell.")
    quantity = _alias(payload, ("qty_shares", "quantity", "qty", "shares"), _positive_int)
    price = _alias(payload, ("limit_price", "price", "limitPrice", "limit"), _positive_decimal)
    notional = _alias(payload, ("notional_usd", "notionalUsd", "dollar_amount", "dollars"),
                      _positive_decimal)
    allow_pre_post = _alias(payload, ("allow_pre_post", "allowPrePost", "extended_hours", "extendedHours"),
                            _boolean, False)
    if kind == "market":
        if price is not None:
            raise CommandError("Market commands cannot include a limit price; the API submits native MO.")
        if allow_pre_post:
            raise CommandError("Captured native Market orders do not support pre/post-session flags.")
        if quantity is None and notional is None:
            raise CommandError("MARKET_ORDER requires qty_shares or notional_usd.")
    elif price is None or quantity is None:
        raise CommandError("LIMIT_ORDER and LIMIT_ORDER_FOK require qty_shares and limit_price.")
    return TradingCommand(command_id, symbol, side, quantity, kind, price, notional, allow_pre_post)


def _timestamp(value):
    if isinstance(value, bool):
        raise CommandError("Command timestamp must be Unix time or an ISO date with timezone.")
    if isinstance(value, (int, float, Decimal)) or (isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)):
        try:
            result = float(value)
        except (ValueError, OverflowError):
            raise CommandError("Command timestamp must be finite.") from None
        if result > 100_000_000_000:
            result /= 1000
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timezone is required")
            result = parsed.astimezone(timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            raise CommandError("Command timestamp must include a valid timezone.") from None
    else:
        raise CommandError("Command timestamp must be Unix time or an ISO date with timezone.")
    if not math.isfinite(result) or result <= 0:
        raise CommandError("Command timestamp must be a finite positive Unix time.")
    return result


def validate_command_age(payload: dict, *, now: float, max_age_seconds: float,
                         kafka_timestamp_ms=None, future_tolerance_seconds: float = 60) -> None:
    """Validate provided producer times and, optionally, Kafka's record timestamp."""
    if (isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0 or not math.isfinite(now)):
        raise CommandError("Command age limit must be a finite positive number.")
    times = [_timestamp(value) for _, value in _present(payload,
             ("timestamp", "timeStamp", "created_at", "createdAt", "issued_at", "issuedAt"))]
    if kafka_timestamp_ms is not None:
        if isinstance(kafka_timestamp_ms, bool) or not isinstance(kafka_timestamp_ms, (int, float)):
            raise CommandError("Kafka record timestamp must be milliseconds since Unix epoch.")
        times.append(_timestamp(kafka_timestamp_ms / 1000))
    for issued in times:
        if issued > now + future_tolerance_seconds:
            raise CommandError("Command timestamp is unexpectedly in the future.")
        if now - issued > max_age_seconds:
            raise CommandError("Command is stale and will not be executed.")
    for _, value in _present(payload, ("expires_at", "expiresAt", "expiry", "expiration")):
        if _timestamp(value) <= now:
            raise CommandError("Command has expired and will not be executed.")


def command_fingerprint(command: TradingCommand | CancelCommand) -> str:
    """Stable semantic payload digest for detecting reuse of an ID for a new order."""
    values = asdict(command)
    values.pop("command_id")
    for key, value in values.items():
        if isinstance(value, Decimal):
            parts = value.as_tuple()
            digits = "".join(str(digit) for digit in parts.digits)
            trimmed = digits.rstrip("0")
            exponent = parts.exponent + len(digits) - len(trimmed)
            values[key] = f"{trimmed}e{exponent}"
    values["command_kind"] = "cancel" if isinstance(command, CancelCommand) else "order"
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
