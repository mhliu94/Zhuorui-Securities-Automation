"""mitmproxy addon: record destinations only; never log credentials or bodies."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

OUTPUT = Path(os.environ.get("ZHUORUI_CAPTURE_PRIVATE") or Path(__file__).resolve().parent / "private") / "connections.jsonl"


def http_connect(flow):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "host": flow.request.host,
        "port": flow.request.port,
    }
    with OUTPUT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
