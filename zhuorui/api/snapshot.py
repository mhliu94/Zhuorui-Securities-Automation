"""Map validated broker account reads to the existing Kafka account schema.

The app's cash detail labels read ``cashAmts``. Top-level ``cashAmt`` is
converted into the selected row's currency and must not be used as cash held.
Amounts remain Decimal until the caller serializes them as JSON numbers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from zhuorui.common.config import config_bool, configured_account_id, nested_config
from zhuorui.api.errors import ApiError


class SnapshotError(ApiError):
    """Account data cannot be represented safely in a control-server snapshot."""


def _decimal(value, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise SnapshotError(f"Invalid numeric {field} in account data.")
    try:
        text = str(value)
        # Broker wire numbers are unformatted units, never display text such
        # as '1,000', '10K', percentages, or numbers with whitespace.
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
            raise InvalidOperation
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        raise SnapshotError(f"Invalid numeric {field} in account data.") from None
    if not result.is_finite():
        raise SnapshotError(f"Nonfinite {field} in account data.")
    return result


def _data(response, kind: str, expected_type):
    if not isinstance(response, dict) or response.get("code") != "000000":
        raise SnapshotError(f"A successful {kind} response is required.")
    value = response.get("data")
    if not isinstance(value, expected_type):
        raise SnapshotError(f"Unexpected {kind} response shape.")
    return value


def _identity(value, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SnapshotError(f"Missing or invalid {field} in account data.")
    return value


def _optional_setting(api, name):
    value = api.get(name)
    return None if value is None else _identity(value, f"api.{name}")


def snapshot_identity(config: dict) -> tuple[str, int, bool]:
    """Resolve the same account labels and trading switch as the UI listener."""
    account_id = configured_account_id(config)
    if not account_id:
        raise SnapshotError("Config account_id is required for holdings publication.")
    account = nested_config(config, "account")
    value = next((v for v in (
        config.get("account_num_id"), account.get("num_id"),
        account.get("numeric_id"), account.get("account_num_id"),
    ) if v is not None and v != ""), None)
    number = _decimal(value, "config account_num_id")
    if number <= 0 or number != number.to_integral_value():
        raise SnapshotError("Config account_num_id must be a positive integer.")
    trading = config_bool(account, "trading_enabled", config_bool(config, "trading_enabled", True))
    return account_id, int(number), trading


def cash_balances(config: dict, cash_response: dict, account_response: dict | None = None) -> dict:
    """Select one broker account and valuation row, then read native balances."""
    api = nested_config(config, "api")
    currency = api.get("cash_currency", "USD")
    if not isinstance(currency, str) or currency not in {"USD", "HKD", "CNY"}:
        raise SnapshotError("api.cash_currency must be USD, HKD, or CNY.")
    expected_account = _optional_setting(api, "fund_account")
    expected_type = _optional_setting(api, "fund_account_type")
    if account_response is not None:
        account = _data(account_response, "account", dict)
        actual_account = _identity(account.get("fundAccount"), "account fundAccount")
        actual_type = _identity(account.get("accType"), "account accType")
        if expected_account is not None and expected_account != actual_account:
            raise SnapshotError("Configured broker fund account does not match the logged-in account.")
        if expected_type is not None and expected_type != actual_type:
            raise SnapshotError("Configured broker account type does not match the logged-in account.")
        expected_account, expected_type = actual_account, actual_type

    rows = _data(cash_response, "cash", list)
    if not rows:
        raise SnapshotError("Cash response contains no account rows.")
    groups = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SnapshotError("Unexpected cash account row.")
        identity = (_identity(row.get("fundAccount"), "cash fundAccount"),
                    _identity(row.get("accType"), "cash accType"))
        groups.setdefault(identity, []).append(row)
    matches = [key for key in groups
               if (expected_account is None or key[0] == expected_account)
               and (expected_type is None or key[1] == expected_type)]
    if len(matches) != 1:
        raise SnapshotError("Cash account is missing or ambiguous; verify api.fund_account and api.fund_account_type.")
    selected = [row for row in groups[matches[0]] if row.get("moneyType") == currency]
    if len(selected) != 1:
        raise SnapshotError("Cash currency row is missing or ambiguous; verify api.cash_currency.")
    amounts = selected[0].get("cashAmts")
    if not isinstance(amounts, dict):
        raise SnapshotError("Cash response is missing the native currency breakdown (cashAmts).")
    # CNH deliberately preserves the existing UI publisher's label, whose
    # tvCNHCash field reads the API's cnCashAmt despite moneyType being CNY.
    return {currency: _decimal(amounts.get(field), f"cashAmts.{field}")
            for currency, field in (("HKD", "hkCashAmt"), ("USD", "usCashAmt"), ("CNH", "cnCashAmt"))}


def security_positions(holdings_response: dict) -> list[dict]:
    data = _data(holdings_response, "holdings", dict)
    rows = data.get("holdList")
    if not isinstance(rows, list):
        raise SnapshotError("Holdings response is missing holdList.")
    result, symbols = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise SnapshotError("Unexpected holdings row.")
        symbol = _identity(row.get("code"), "holding code").upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{0,31}", symbol):
            raise SnapshotError("Unsupported symbol format in holdings.")
        quantity = _decimal(row.get("currentAmount"), "holding currentAmount")
        # currentAmount is held shares, not enableAmount (available to sell).
        if quantity == 0:
            continue
        if symbol in symbols:
            raise SnapshotError("Duplicate holding symbols cannot be represented in the control-server schema.")
        symbols.add(symbol)
        # Preserve the broker's signed cost basis; accounting adjustments can
        # make it differ from a positive last-trade price.
        average = _decimal(row.get("costPrice"), "holding costPrice")
        result.append({"symbol": symbol, "qty": quantity, "avg_price": average})
    return result


def account_snapshot(config: dict, holdings_response: dict, cash_response: dict,
                     account_response: dict | None = None, *, now: datetime | None = None) -> dict:
    account_id, account_num_id, trading_enabled = snapshot_identity(config)
    cash = cash_balances(config, cash_response, account_response)
    stamp = now or datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise SnapshotError("Snapshot timestamp must be timezone-aware.")
    return {
        "account_id": account_id,
        "account_num_id": account_num_id,
        "cash": cash["USD"],
        "cash_by_currency": cash,
        "positions": security_positions(holdings_response),
        "ts": stamp.astimezone(timezone.utc).isoformat(),
        "trading_enabled": trading_enabled,
    }
