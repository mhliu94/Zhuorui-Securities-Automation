"""Unsigned request plans from captured flows and the app's Buy/Sell enum.

Planning has no account, signing, network, Kafka or Android dependencies.
"""
from decimal import Decimal, InvalidOperation
import math
import re
from .errors import ApiError


def plan_order(symbol, side, quantity, kind, *, price=None, allow_pre_post=False, cancel_after=1):
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9.=\-]{1,16}", symbol):
        raise ApiError("Invalid US stock symbol.")
    if side not in {"buy", "sell"}:
        raise ApiError("Order side must be buy or sell.")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ApiError("Quantity must be a positive whole number of shares.")
    if kind not in {"market", "limit", "timed-cancel"}:
        raise ApiError("Order type must be market, limit or timed-cancel.")
    payload = {"volumeMultiple": 1, "entrustProp": "MO" if kind == "market" else "LO",
               "code": symbol.upper(), "entrustBs": "1" if side == "buy" else "2", "entrustAmount": quantity,
               "apStatus": 0, "ts": "US"}
    if kind == "market":
        if price is not None or allow_pre_post:
            raise ApiError("Captured Market orders omit limit price and pre/post-session flags.")
    else:
        try:
            value = Decimal(str(price))
        except (InvalidOperation, ValueError):
            raise ApiError("Limit and timed-cancel plans require a positive decimal price.") from None
        if not value.is_finite() or value <= 0:
            raise ApiError("Price must be a finite positive decimal.")
        payload.update(entrustPrice=value, allowPrePost="Y" if allow_pre_post else "N")
    plan = {"mode": "offline_plan", "path": "/as_trade/api/order/v1/entrust_enter",
            "unsigned_body": payload, "request_sent": False}
    if kind == "timed-cancel":
        if isinstance(cancel_after, bool) or not isinstance(cancel_after, (float, int)) or not math.isfinite(cancel_after) or cancel_after <= 0:
            raise ApiError("Cancellation delay must be a finite positive number.")
        plan["cancellation"] = {"delay_seconds_from_dispatch": cancel_after,
            "reference_source": "Acknowledged orderTxnReference; reconcile unknown outcomes first",
            "partial_fills_possible": True, "native_fok": False}
    return plan


def plan_cancel(reference):
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 256 or any(ord(c) < 32 for c in reference):
        raise ApiError("A valid order transaction reference is required.")
    return {"mode": "offline_plan", "path": "/as_trade/api/order/v1/entrust_withdraw",
            "unsigned_body": {"orderTxnReference": reference}, "request_sent": False}
