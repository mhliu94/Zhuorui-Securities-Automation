"""Password login on the imported device identity; credentials never leave config memory."""
import hashlib
import re
import time

from zhuorui.common.config import config_login_phone, config_login_password, config_string
from .client import ApiClient
from .errors import ApiError, BrokerRejected, LoginBlocked, SessionError
from .session import validate_identity, save_session, load_session


def password_login(config, settings, previous, *, client_factory=ApiClient, now=time.time):
    phone, password = config_login_phone(config), config_login_password(config)
    phone_area = config_string(config, "login", "phone_area") or "86"
    if not phone or not password:
        raise LoginBlocked("Configure login.phone and login.password for automatic login.")
    if not re.fullmatch(r"[0-9]{5,20}", phone) or not re.fullmatch(r"[0-9]{1,4}", phone_area):
        raise LoginBlocked("Login phone or country calling code is not configured correctly.")
    try:
        result = client_factory(previous, settings).password_login(phone, password, phone_area=phone_area)
    except BrokerRejected as exc:
        if exc.code == "010007":
            raise LoginBlocked("Zhuorui requires phone verification for this device. Complete verification in the emulator before retrying.") from None
        raise LoginBlocked(f"Password login was rejected (code {exc.code or 'unavailable'}). Check credentials or account verification before retrying.") from None
    except SessionError:
        raise LoginBlocked("Zhuorui refused password login. Check account verification in the emulator before retrying.") from None
    if not isinstance(result, dict) or result.get("code") != "000000":
        raise LoginBlocked("Login response did not establish a successful login.")
    data = result.get("data")
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and re.fullmatch(r"[0-9a-fA-F]{32}", data[k]) for k in ("userId", "token")):
        raise LoginBlocked("Login response did not contain a validated session; account verification may be required.")
    if data.get("phone") != phone or data.get("phoneArea") != phone_area:
        raise LoginBlocked("Login response belongs to a different phone identity; refusing to use it.")
    if data["userId"] != previous["headers"]["userid"]:
        raise LoginBlocked("Login returned a different broker user; refusing to switch accounts.")
    current_time = now()
    session = {**previous, "headers": {**previous["headers"], "userid": data["userId"], "token": data["token"]},
               "captured_at": current_time, "imported_at": current_time, "source": "api_password_login",
               "generation": hashlib.sha256(data["token"].encode()).hexdigest()}
    try:
        if settings.session_file.exists():
            current = load_session(settings.session_file, config)
            if current["headers"]["token"] != previous["headers"]["token"]:
                raise ApiError("The saved session changed during login; check the replacement session before retrying.")
        validate_identity(session, config, settings)
    except SessionError:
        raise LoginBlocked("Recovered session failed the configured account/device identity checks.") from None
    # Save the returned token before subsequent reads: a read timeout must not
    # discard a successful login or cause another password submission.
    save_session(settings.session_file, session)
    return session
