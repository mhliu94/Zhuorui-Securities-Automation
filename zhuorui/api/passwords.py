"""Password transforms used by the validated Android app build.

These functions only transform data in memory; they never perform a login,
unlock trading, persist credentials, or make network requests.
"""
import hashlib
import secrets

# Public encryption key from Sm2Util.publicKeyHexToBytes in Android 3.1.5.
# This is the broker's public key, not an account credential.
TRADE_PUBLIC_KEY = bytes.fromhex(
    "04eb316a6de2c8a590c92fdb216cc5507b8bc772940941aca5ae0491937ab23ef3e"
    "e70f4fcac903d40ef2e59d32dd70fe0f1c41449fae6453d28930d01916ced69"
)

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


def trade_password_ciphertext(password: str) -> str:
    """Return app-compatible SM2/SM3 C1 || C3 || C2 without the 04 prefix.

    Use fresh OS randomness on every call. Neither the plaintext nor the
    ciphertext belongs in logs, journal entries, or status events.
    """
    from gmalg import SM2, PC_MODE

    plaintext = _password_bytes(password)
    try:
        cipher = SM2(pk=TRADE_PUBLIC_KEY, rnd_fn=secrets.randbits,
                     pc_mode=PC_MODE.RAW).encrypt(plaintext)
        if len(cipher) != 97 + len(plaintext) or cipher[:1] != b"\x04":
            raise ValueError()
    except Exception:
        raise ApiError("Trading-password encryption failed; check the API dependencies.") from None
    return cipher[1:].hex()
