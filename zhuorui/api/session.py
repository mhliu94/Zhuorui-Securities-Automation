"""Import an existing app session; keep credentials encrypted with Windows DPAPI.

Capture import is local. Neither importer logs in or renews a token.
mitmproxy is loaded only by the capture importer, not by the API runtime.
"""
import base64
import ctypes
import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from zhuorui.common.config import configured_account_id, configured_account_num_id, config_string
from .errors import SessionError
from .signing import find_signing_key

HOST = "backendpro.zr.hk"
READ_PATHS = {
    "holdings": "/as_trade/api/order/v1/get_hold_list",
    "cash": "/as_trade/api/funds/v1/info",
    "orders": "/as_trade/api/order/v1/get_today_entrust",
    "account": "/as_trade/api/account/v1/info",
    "trade-auth": "/as_trade/api/auth/v1/current_auth_info",
}


def binding(config):
    return {"account_id": configured_account_id(config),
            "account_num_id": configured_account_num_id(config),
            "server_id": config.get("server_id")}


def protect(data, *, decrypt=False):
    if os.name != "nt":
        raise SessionError("Session storage requires Windows DPAPI under the importing Windows user.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise SessionError("Could not access the encrypted session for this Windows user.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(dest.data)


def save_session(path, session):
    encrypted = protect(json.dumps(session, allow_nan=False).encode("utf-8"))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def load_session(path, config):
    try:
        session = json.loads(protect(Path(path).read_bytes(), decrypt=True))
    except FileNotFoundError:
        raise SessionError("No imported session. Refresh the app account page, then run import-session.") from None
    except (ValueError, OSError):
        raise SessionError("Cannot read the session file. Import a fresh session under this Windows user.") from None
    if not isinstance(session, dict) or session.get("version") != 1:
        raise SessionError("Unsupported session file; import the session again.")
    if session.get("binding") != binding(config):
        raise SessionError("Session belongs to different configured account/server identifiers.")
    headers = session.get("headers")
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) and not any(c in k + v for c in "\r\n\0") for k, v in headers.items()):
        raise SessionError("Invalid session headers; import the session again.")
    if not all(headers.get(k) for k in ("token", "userid", "deviceid")) or not isinstance(session.get("signing_key"), str):
        raise SessionError("Session is incomplete; import a successful account query.")
    check_expected_user(session, config)
    return session


def check_expected_user(session, config):
    expected = config_string(config, "api", "expected_user_id")
    if expected and expected != session["headers"]["userid"]:
        raise SessionError("Broker user differs from api.expected_user_id; refusing to use this account.")


def validate_identity(session, config, settings):
    check_expected_user(session, config)
    if settings.session_file.exists():
        previous = load_session(settings.session_file, config)
        if any(previous["headers"][key] != session["headers"][key] for key in ("userid", "deviceid")):
            raise SessionError("Account/device differs from the imported session; refusing an automatic switch. Use a separate config and session file for another account.")
        if previous["captured_at"] > session["captured_at"]:
            raise SessionError("Refusing to replace a newer session with an older capture.")


def session_from_flows(flows, config, settings, *, now=None):
    now = time.time() if now is None else now
    selected = None
    invalid = {}
    for flow in sorted(flows, key=lambda f: f.request.timestamp_start):
        request = flow.request
        if request.host != HOST or request.scheme != "https" or request.port != 443 or not flow.response:
            continue
        try:
            body = json.loads(flow.response.get_text())
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        token = request.headers.get("token")
        if token and body.get("code") in {"000102", "000112"}:
            invalid[token] = request.timestamp_start
        if request.method == "POST" and request.path in READ_PATHS.values() and token:
            # Do not fall back to an older successful read after a newer failure.
            selected = (flow, body)
    if selected is None:
        raise SessionError("No authenticated account query captured. Refresh holdings or Today's Orders in the app.")
    flow, body = selected
    request = flow.request
    if flow.response.status_code != 200 or body.get("code") != "000000":
        raise SessionError("Latest captured account query failed. Restore the app login and refresh again.")
    if invalid.get(request.headers.get("token"), float("-inf")) >= request.timestamp_start:
        raise SessionError("Captured session was invalidated. Restore the app login and capture a fresh account query.")
    payload = json.loads(request.get_text(), parse_float=Decimal)
    if not isinstance(payload, dict):
        raise SessionError("Invalid captured request body.")
    stamp = payload.get("timeStamp")
    if isinstance(stamp, bool) or not isinstance(stamp, int) or not -60 <= now - stamp / 1000 <= settings.session_max_capture_age_seconds:
        raise SessionError("Capture is stale. Refresh holdings or Today's Orders, then import again.")
    sig = payload.pop("sign", None)
    if not isinstance(sig, str):
        raise SessionError("Captured request signature is missing.")
    key = find_signing_key(settings.apk_file, payload, sig)
    from cryptography.hazmat.primitives import serialization
    headers = {k.lower(): v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}}
    if not all(headers.get(k) for k in ("token", "userid", "deviceid")):
        raise SessionError("Capture lacks account or device identity.")
    return {"version": 1, "binding": binding(config), "headers": headers,
            "captured_at": request.timestamp_start, "imported_at": now,
            "generation": hashlib.sha256(headers["token"].encode()).hexdigest(),
            "signing_key": base64.b64encode(key.private_bytes(serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode("ascii")}


def import_session(config, settings):
    try:
        from mitmproxy import io
    except ImportError:
        raise SessionError("Session import needs capture dependencies. Use api_research/.venv/Scripts/python.exe for this command.") from None
    try:
        with settings.capture_file.open("rb") as stream:
            session = session_from_flows(list(io.FlowReader(stream).stream()), config, settings)
    except FileNotFoundError:
        raise SessionError("Capture or APK file missing. Complete the local capture setup first.") from None
    validate_identity(session, config, settings)
    save_session(settings.session_file, session)
    return {"session_imported": True, "signature_verified": True, "network_requests_sent": 0}
