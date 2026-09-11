"""On-demand trading authorization before an order or cancellation is dispatched."""
from zhuorui.common.config import config_trade_password
from .errors import ApiError, BrokerRejected, SessionError


def _data(response):
    if not isinstance(response, dict) or response.get("code") != "000000":
        raise ApiError("Trading authorization returned an unsupported response.")
    return response.get("data")


def _identity(value):
    return isinstance(value, str) and bool(value) and value == value.strip() and len(value) <= 256


def _authorized(response, client_id, user_id):
    data = _data(response)
    if data is None or data == {}:
        return False
    if not isinstance(data, dict):
        raise ApiError("Trading authorization returned an unsupported response.")
    if data.get("accountId") != client_id or data.get("userId") != user_id:
        raise ApiError("Trading authorization does not match the current account and user.")
    return True


class TradeAuthorizer:
    """One attempt while locked; failures pause attempts for this executor.

    A subsequent verified unlocked response clears the pause. Otherwise correct
    the password or account verification and restart the listener. Login recovery
    does not reset this pause or replay the rejected order.
    """
    def __init__(self, config):
        self.config = config
        self.blocked = False

    def ensure(self, client, account, auth):
        data = _data(account)
        client_id = data.get("clientId") if isinstance(data, dict) else None
        user_id = client.headers.get("userid")
        if not _identity(client_id) or not _identity(user_id):
            raise ApiError("Account query did not establish the trading client and user identity.")
        if _authorized(auth, client_id, user_id):
            self.blocked = False
            return
        if self.blocked:
            raise ApiError("Automatic trading unlock is paused after a failed attempt. Check trade_password or account verification, then restart the listener.")
        password = config_trade_password(self.config)
        if not password:
            raise ApiError("Trading authorization is locked. Configure trade_password or unlock trading in the app.")
        # Reserve the attempt before sending. A timeout must not cause a stream
        # of subsequent commands to submit the same password over and over.
        self.blocked = True
        try:
            result = client.unlock_trading(client_id, password)
            unlocked = _data(result)
            if not isinstance(unlocked, dict) or unlocked.get("userId") != user_id:
                raise ApiError("Trading unlock did not confirm the current user.")
            # forceChangePwd=1 was present in captured successful app unlocks;
            # use the explicit authorization query as the source of readiness.
            if not _authorized(client.query("trade-auth"), client_id, user_id):
                raise ApiError("Trading authorization is still locked after the password request. Check account verification in the app.")
        except BrokerRejected as exc:
            if exc.code == "350117":
                raise ApiError("Trading password was rejected (code 350117). Check trade_password; automatic unlock is paused.") from None
            raise ApiError("Trading unlock was rejected by the broker. Check account verification; automatic unlock is paused.") from None
        except SessionError:
            raise  # Let the executor schedule ordinary session recovery.
        except ApiError:
            raise
        except Exception:
            raise ApiError("Trading unlock could not be verified; no order was sent. Automatic unlock is paused.") from None
        self.blocked = False
