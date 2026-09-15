"""Small, flushed runtime logs without exception messages or private payloads."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading


_lock = threading.RLock()


def error_details(error):
    """Keep error types and code locations, never messages, source or locals.

    Broker/HTTP exceptions may contain tokens, request bodies or credentials.
    Follow even suppressed contexts to retain the underlying transport error.
    """
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 5:
        seen.add(id(error))
        frames, trace = [], error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append(f"{Path(code.co_filename).name}:{trace.tb_lineno}:{code.co_name}")
            trace = trace.tb_next
        item = {"type": type(error).__name__, "frames": frames[-8:]}
        for key in ("errno", "winerror", "code"):
            value = getattr(error, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                item[key] = value
            elif key == "code" and isinstance(value, str) and len(value) == 6 and value.isascii() and value.isdigit():
                item[key] = value  # Broker result codes, without its response body.
        result.append(item)
        error = error.__cause__ or error.__context__
    return result


def log_event(component, message, *, level="INFO", error=None, **fields):
    """Callers supply only reviewed operational fields, never raw input/config."""
    try:
        if error is not None:
            fields["errors"] = error_details(error)
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        details = json.dumps(fields, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        line = (f"{stamp} {level} pid={os.getpid()} thread={threading.current_thread().name} "
                f"{component}: {message} {details}")
        with _lock:
            print(line, flush=True)
    except Exception:
        # A closed output stream or full disk must not interrupt live operations.
        pass
