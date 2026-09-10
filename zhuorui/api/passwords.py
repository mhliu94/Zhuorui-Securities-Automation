"""Password transforms used by the validated Android app build.

These functions only transform data in memory; they never perform a login,
unlock trading, persist credentials, or make network requests.
"""
import hashlib

from .errors import ApiError


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str) or not password:
        raise ApiError("A nonempty password is required.")
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        raise ApiError("Password is not valid UTF-8 text.") from None
    if len(encoded) > 4096:
        raise ApiError("Password exceeds the supported length.")
    return encoded


def login_password_hash(password: str) -> str:
    """Return LoginRequest.loginPassword: lowercase MD5 of UTF-8 plaintext.

    This matches the app's wire protocol and is not a password-storage scheme.
    Keep the returned value private, just like the input password.
    """
    return hashlib.md5(_password_bytes(password), usedforsecurity=False).hexdigest()
