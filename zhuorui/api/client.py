"""Signed account reads and typed order requests using the existing app login.

No automatic write retry or redirect is performed. Password login is a separate
typed operation invoked only by the listener's recovery schedule.
"""
import base64
import json
import http.client
import ssl
import time
import urllib.error
import urllib.request
from decimal import Decimal
import re

from .errors import ApiError, SessionExpired, LoggedInElsewhere, BrokerRejected, OrderOutcomeUnknown
from .orders import plan_order, plan_cancel
from .session import HOST, READ_PATHS
from .signing import canonical, signature

QUOTE_PATH = "/as_market/api/stock_price/v1/get_prices"
LOGIN_PATH = "/as_user/api/user_account/v1/user_login_pwd"
TRADE_AUTH_PATH = "/as_trade/api/account/v1/auth"


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response key")
        result[key] = value
    return result


def nonfinite(_):
    raise ValueError("Nonfinite response number")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("Broker redirect refused.")


class ApiClient:
    def __init__(self, session, settings, *, opener=None, now=time.time):
        from cryptography.hazmat.primitives import serialization
        try:
            self.key = serialization.load_der_private_key(base64.b64decode(session["signing_key"], validate=True), password=None)
        except (ValueError, KeyError, TypeError):
            raise ApiError("Session signing material is invalid; import a fresh session.") from None
        self.headers = dict(session["headers"])
        self.settings = settings
        self.now = now
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())

    def query(self, name):
        if name not in READ_PATHS:
            raise ApiError("This runtime permits only named account queries.")
        return self._request(READ_PATHS[name], {})

    def submit_order(self, symbol, side, quantity, kind, *, price=None, allow_pre_post=None):
        plan = plan_order(symbol, side, quantity, kind, price=price, allow_pre_post=allow_pre_post)
        return self._request(plan["path"], plan["unsigned_body"], write=True)

    def cancel_order(self, reference):
        plan = plan_cancel(reference)
        return self._request(plan["path"], plan["unsigned_body"], write=True)

    def password_login(self, phone, password, *, phone_area="86"):
        from .passwords import login_password_hash
        if not isinstance(phone, str) or not re.fullmatch(r"[0-9]{5,20}", phone):
            raise ApiError("Configure a valid login.phone before automatic login.")
        if not isinstance(phone_area, str) or not re.fullmatch(r"[0-9]{1,4}", phone_area):
            raise ApiError("Configure login.phone_area as a numeric country calling code.")
        return self._request(LOGIN_PATH, {"phone": phone, "phoneArea": phone_area,
            "accountType": 1, "type": 1, "loginPassword": login_password_hash(password)}, login=True)

    def unlock_trading(self, client_id, password):
        """Authorize the current account; never retry a password submission."""
        from .passwords import trade_password_ciphertext
        if not isinstance(client_id, str) or not client_id or client_id != client_id.strip() or len(client_id) > 256:
            raise ApiError("Account query did not provide a valid trading client ID.")
        return self._request(TRADE_AUTH_PATH,
                             {"clientId": client_id, "password": trade_password_ciphertext(password)},
                             trade_auth=True)

    def quantity_for_notional(self, symbol, budget):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.=\-]{1,16}", symbol):
            raise ApiError("Invalid US symbol for quantity sizing.")
        result = self._request(QUOTE_PATH, {"stockVos": [{"code": symbol, "ts": "US"}]})
        rows = result.get("data")
        if not isinstance(rows, list):
            raise ApiError("Quote response is unavailable for Market quantity sizing.")
        matches = [row for row in rows if isinstance(row, dict) and row.get("code") == symbol and row.get("ts") == "US"]
        if len(matches) != 1:
            raise ApiError("Quote response does not identify exactly one US stock.")
        row = matches[0]
        stamp, last = row.get("time"), row.get("last")
        if row.get("delay") is not False:
            raise ApiError("Broker quote is delayed. Send qty_shares for a true Market order; notional sizing needs a fresh real-time quote.")
        if isinstance(row.get("suspension"), bool) or row.get("suspension") not in (1, 3):
            raise ApiError("Quote does not establish an actively trading instrument.")
        if isinstance(stamp, bool) or not isinstance(stamp, int) or not -5 <= self.now() - stamp / 1000 <= self.settings.quote_max_age_seconds:
            raise ApiError("Quote is stale; send an explicit share quantity or refresh the real-time quote source.")
        try:
            if isinstance(last, bool) or isinstance(budget, bool):
                raise ValueError()
            price, notional = Decimal(str(last)), Decimal(str(budget))
            if not price.is_finite() or price <= 0 or not notional.is_finite() or notional <= 0:
                raise ValueError()
            for value in (price, notional):
                parts = value.as_tuple()
                if len(parts.digits) > 128 or abs(parts.exponent) > 100:
                    raise ValueError()
            # Decimal division can round up before flooring near a whole share.
            budget_num, budget_den = notional.as_integer_ratio()
            price_num, price_den = price.as_integer_ratio()
            quantity = (budget_num * price_den) // (budget_den * price_num)
        except (ValueError, ArithmeticError):
            raise ApiError("Invalid quote or notional amount for quantity sizing.") from None
        if quantity <= 0:
            raise ApiError("Notional amount is below one share at the reference quote.")
        return quantity

    def _request(self, path, body, *, write=False, login=False, trade_auth=False):
        if sum((bool(write), bool(login), bool(trade_auth))) > 1:
            raise ApiError("Unsupported mixed broker operation.")
        permitted = {TRADE_AUTH_PATH} if trade_auth else {LOGIN_PATH} if login else ({"/as_trade/api/order/v1/entrust_enter", "/as_trade/api/order/v1/entrust_withdraw"} if write else set(READ_PATHS.values()) | {QUOTE_PATH})
        if path not in permitted:
            raise ApiError("Unsupported broker operation.")
        payload = {**body, "timeStamp": int(self.now() * 1000)}
        payload["sign"] = signature(self.key, payload)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
                   and not (login and k.lower() in {"token", "userid", "authorization", "cookie"})}
        headers["content-type"] = "application/json; charset=utf-8"
        request = urllib.request.Request("https://" + HOST + path,
            data=canonical(payload), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.settings.request_timeout_seconds) as response:
                if response.status != 200:
                    raise (OrderOutcomeUnknown if write else ApiError)("Broker returned an unexpected HTTP status; no retry was attempted.")
                result = json.load(response, parse_float=Decimal, object_pairs_hook=strict_object, parse_constant=nonfinite)
        except urllib.error.HTTPError as exc:
            raise (OrderOutcomeUnknown if write else ApiError)(f"Broker returned HTTP {exc.code}; no retry was attempted.") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise (OrderOutcomeUnknown if write else ApiError)("Broker request could not complete; no retry was attempted.") from None
        except (ValueError, UnicodeError):
            raise (OrderOutcomeUnknown if write else ApiError)("Broker returned invalid JSON; no retry was attempted.") from None
        except ApiError:
            if write:
                raise OrderOutcomeUnknown("Broker response could not be followed safely; no retry was attempted.") from None
            raise
        if not isinstance(result, dict):
            raise (OrderOutcomeUnknown if write else ApiError)("Broker returned an unexpected response shape.")
        code = result.get("code")
        if code == "000102":
            raise SessionExpired("Session is invalid. Restore the emulator login and import its fresh session.")
        if code == "000112":
            raise LoggedInElsewhere("Session was displaced by another login. Account operations paused.")
        if code != "000000":
            if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
                raise (OrderOutcomeUnknown if write else ApiError)("Broker response is missing a valid result code.")
            raise BrokerRejected(f"Broker rejected the request (code {code}).", code=code)
        return result
