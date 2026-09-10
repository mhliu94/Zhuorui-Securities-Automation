"""Capture Zhuorui HTTP flows privately; emit a separate schema-only index.

Use with a verified capture session and --allow-hosts for the observed broker
host. The full .mitm capture contains secrets; never commit or share it.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from mitmproxy import io

PRIVATE = Path(os.environ.get("ZHUORUI_CAPTURE_PRIVATE") or Path(__file__).resolve().parent / "private")
HOST = "backendpro.zr.hk"


def http_connect(flow):
    PRIVATE.mkdir(exist_ok=True)
    with (PRIVATE / "test-connections.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"host": flow.request.host, "port": flow.request.port}) + "\n")


def shape(value, depth=0):
    if depth > 8:
        return "nested"
    if isinstance(value, dict):
        return {key: shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def body_shape(message):
    try:
        return shape(json.loads(message.get_text(strict=False)))
    except (ValueError, TypeError):
        return "non-json"


def request(flow):
    """Persist the outgoing request even if no response ever arrives."""
    if flow.request.host != HOST:
        return
    PRIVATE.mkdir(exist_ok=True)
    with (PRIVATE / "zhuorui-requests.mitm").open("ab") as stream:
        io.FlowWriter(stream).add(flow)


def record_completed(flow):
    if flow.request.host != HOST:
        return
    PRIVATE.mkdir(exist_ok=True)
    with (PRIVATE / "zhuorui-flows.mitm").open("ab") as stream:
        io.FlowWriter(stream).add(flow)
    index = {
        "flow_id": flow.id,
        "time": datetime.now(timezone.utc).isoformat(),
        "request_started": flow.request.timestamp_start,
        "request_ended": flow.request.timestamp_end,
        "response_started": flow.response.timestamp_start if flow.response else None,
        "response_ended": flow.response.timestamp_end if flow.response else None,
        "method": flow.request.method,
        "host": flow.request.host,
        "path": flow.request.path.split("?", 1)[0],
        "status": flow.response.status_code if flow.response else None,
        "transport_error": flow.error is not None,
        "request_headers": list(flow.request.headers.keys()),
        "request_shape": body_shape(flow.request),
        "response_shape": body_shape(flow.response) if flow.response else None,
    }
    with (PRIVATE / "api-index.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(index) + "\n")


def response(flow):
    record_completed(flow)


def error(flow):
    record_completed(flow)
