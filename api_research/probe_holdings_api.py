"""Inspect or repeat one captured, successful, authenticated account read.

Default is offline inspection. --send permits one fresh-signed request to an
exact allowlisted holdings/cash/assets path only. No login, order submission,
amendment, cancellation, Kafka publication, retries, or redirects are supported.
"""
import argparse
import json
import math
import ssl
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

import certifi
from mitmproxy import io

from probe_public_api import HOST, NoRedirect, canonical, find_signing_key, signature

ROOT = Path(__file__).resolve().parent
READ_PATHS = {
    "holdings": frozenset({
        "/as_trade/api/order/v1/get_hold_list",
        "/as_trade/api/client_asset/v1/hold_list",
    }),
    "cash": frozenset({"/as_trade/api/funds/v1/info"}),
    "assets": frozenset({"/as_trade/api/client_asset/v1/client_asset_detail"}),
}
MAX_CAPTURE_AGE_SECONDS = 600


def validate_capture(flow, query, *, now=None):
    request = flow.request
    if request.host != HOST or request.scheme != "https" or request.port != 443:
        raise ValueError("Unexpected capture destination")
    if request.method != "POST" or request.path not in READ_PATHS[query]:
        raise ValueError("This probe permits only the selected account-read paths")
    if not flow.response or flow.response.status_code != 200:
        raise ValueError("A successful captured response is required")
    response = json.loads(flow.response.get_text(strict=True))
    if not isinstance(response, dict) or response.get("code") != "000000":
        raise ValueError("The captured query did not report application success")
    if not request.headers.get("token"):
        raise ValueError("A captured authenticated session token is required")
    payload = json.loads(request.get_text(strict=True), parse_float=Decimal)
    if not isinstance(payload, dict) or not isinstance(payload.get("sign"), str):
        raise ValueError("Missing captured request signature")
    stamp = payload.get("timeStamp")
    if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp <= 0:
        raise ValueError("Missing captured millisecond timestamp")
    if now is not None:
        # The signed timestamp proves the request's age, independent of when
        # a previously captured response may have been saved to disk.
        age = now - stamp / 1000
        if not math.isfinite(age) or age < -60 or age > MAX_CAPTURE_AGE_SECONDS:
            raise ValueError("Capture is stale; refresh holdings in the app and recapture")
    return payload


def select_capture(path, query):
    selected = None
    with Path(path).open("rb") as stream:
        for flow in io.FlowReader(stream).stream():
            if flow.request.host == HOST and flow.request.path in READ_PATHS[query]:
                # Choose the most recent relevant observation, even if it is a
                # failure. Never fall back to an older successful auth session.
                if selected is None or flow.request.timestamp_start > selected.request.timestamp_start:
                    selected = flow
    if selected is None:
        raise ValueError("No authenticated account-query capture is available yet")
    return selected


def prepare_query(flow, query, *, now=None):
    payload = validate_capture(flow, query, now=now)
    captured_signature = payload.pop("sign")
    key = find_signing_key(ROOT / "private/zhuorui.apk", payload, captured_signature)
    if now is not None:
        payload["timeStamp"] = int(now * 1000)
    payload["sign"] = signature(key, payload)
    headers = {
        name: value for name, value in flow.request.headers.items()
        if name.lower() not in {"content-length", "accept-encoding", "connection", "host"}
    }
    return urllib.request.Request(
        "https://" + HOST + flow.request.path,
        data=canonical(payload), headers=headers, method="POST",
    )


def run_probe(query, *, send=False):
    flow = select_capture(ROOT / "private/zhuorui-flows.mitm", query)
    request = prepare_query(flow, query, now=time.time() if send else None)
    if not send:
        return {"query": query, "path": flow.request.path, "signature_verified": True,
                "request_sent": False, "note": "Freshness is checked again when --send is used"}
    context = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect(),
    )
    started = time.monotonic()
    # Deliberately no retries and no fallback authentication flow.
    with opener.open(request, timeout=20) as reply:
        body = json.load(reply)
        status = reply.status
    if not isinstance(body, dict) or body.get("code") != "000000":
        raise ValueError("Account query was not successful; recapture the account session")
    output = ROOT / "private" / ("account-" + query + "-result.json")
    output.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"query": query, "path": flow.request.path, "http_status": status,
            "application_code": body["code"], "elapsed_seconds": round(time.monotonic() - started, 3),
            "request_sent": True, "private_output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", choices=tuple(READ_PATHS), default="holdings")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    try:
        result = run_probe(args.query, send=args.send)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        # Messages generated by this module contain no account values/tokens.
        print(json.dumps({"ready": False, "reason": str(exc)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
