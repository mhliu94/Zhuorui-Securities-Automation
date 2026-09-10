"""Verify the captured signature and optionally send ONE public version query.

This is a bounded feasibility probe, not a trading client. It cannot log in,
query holdings, place orders, cancel orders, or replay arbitrary capture paths.
Key material stays inside the ignored APK and process memory.
"""
import argparse
import sys
import base64
import json
import ssl
import time
import urllib.request
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zhuorui.api.signing import canonical, signature, find_signing_key

import certifi
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from mitmproxy import io

ROOT = Path(__file__).resolve().parent
HOST = "backendpro.zr.hk"
ENDPOINT = "/as_common/api/app_version/v1/get_new_version"






class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Refusing to redirect the public API probe")




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send one fresh signed public version query")
    args = parser.parse_args()
    selected = None
    with (ROOT / "private/zhuorui-flows.mitm").open("rb") as stream:
        for flow in io.FlowReader(stream).stream():
            if flow.request.host == HOST and flow.request.path == ENDPOINT:
                selected = flow
    if selected is None:
        raise RuntimeError("First capture the test app's public version query")
    if selected.request.method != "POST":
        raise RuntimeError("Unexpected captured method")
    if any(k.lower() in {"token", "userid", "authorization", "cookie", "tradetoken"} for k in selected.request.headers):
        raise RuntimeError("Refusing a capture with account authentication headers")
    payload = json.loads(selected.request.get_text())
    if set(payload) != {"timeStamp", "sign"}:
        raise RuntimeError("Unexpected version-query payload")
    captured_signature = payload.pop("sign")
    matching_key = find_signing_key(ROOT / "private/zhuorui.apk", payload, captured_signature)
    print("Captured signature reproduced exactly using sorted JSON and RSA/SHA-1.")
    if not args.send:
        print("Offline verification only. No request sent.")
        return
    payload["timeStamp"] = time.time_ns() // 1_000_000
    payload["sign"] = signature(matching_key, payload)
    headers = {
        name: value for name, value in selected.request.headers.items()
        if name.lower() not in {"content-length", "accept-encoding", "connection", "host"}
    }
    request = urllib.request.Request("https://" + HOST + ENDPOINT, data=canonical(payload), headers=headers, method="POST")
    context = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect())
    started = time.monotonic()
    with opener.open(request, timeout=20) as response:
        body = json.load(response)
        result = {
            "host": HOST, "path": ENDPOINT,
            "http_status": response.status,
            "application_code": body.get("code"),
            "response_keys": sorted(body),
            "data_keys": sorted(body["data"]) if isinstance(body.get("data"), dict) else [],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "fresh_signature": True, "authenticated": False,
            "proxy_used": False,
        }
    (ROOT / "private/public-probe-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
