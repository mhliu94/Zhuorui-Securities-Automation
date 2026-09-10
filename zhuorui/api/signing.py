"""Exact JSON signing reproduced from captured app requests."""
import base64
import json
from decimal import Decimal
from zipfile import ZipFile

def canonical(payload):
    # Fastjson preserves BigDecimal scale. The captured Limit price retains
    # trailing zeroes; parsing it as a float changes the signed bytes.
    def render(value):
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("Non-finite decimal is not valid signed JSON")
            return str(value)
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise ValueError("Signed JSON object keys must be strings")
            return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + render(value[key]) for key in sorted(value)) + "}"
        if isinstance(value, (list, tuple)):
            return "[" + ",".join(render(item) for item in value) + "]"
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    return render(payload).encode("utf-8")


def signature(key, payload):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return base64.b64encode(key.sign(canonical(payload), padding.PKCS1v15(), hashes.SHA1())).decode("ascii")


def find_signing_key(apk_path, payload, captured_signature):
    """Find app signing material by reproducing an observed signature locally."""
    from cryptography.hazmat.primitives import serialization
    with ZipFile(apk_path) as apk:
        for name in apk.namelist():
            if not name.startswith("assets/config") or not name.endswith(".properties"):
                continue
            properties = apk.read(name).decode("utf-8-sig").replace("\\\r\n", "").replace("\\\n", "")
            for line in properties.splitlines():
                key_name, sep, value = line.partition("=")
                if sep and key_name.strip() == "private_key":
                    key = serialization.load_der_private_key(base64.b64decode(value.strip()), password=None)
                    if signature(key, payload) == captured_signature:
                        return key
    raise RuntimeError("No matching app signing key; do not send guessed signatures")
