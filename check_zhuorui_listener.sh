#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=zhuorui_shell_common.sh
source "$ROOT/zhuorui_shell_common.sh"
PID_PATH="$ROOT/zhuorui_listener.pid"
CURRENT_RUN_PATH="$ROOT/zhuorui_listener.current.json"

if [[ ! -f $PID_PATH ]]; then
    echo "Zhuorui listener is not running: no PID file found."
    exit 1
fi
PID_VALUE=$(tr -d '[:space:]' < "$PID_PATH")
if ! valid_pid "$PID_VALUE"; then
    echo "Zhuorui listener status is unknown: PID file is invalid."
    exit 1
fi
if ! process_is_running "$PID_VALUE"; then
    echo "Zhuorui listener is not running. Stale PID file: $PID_VALUE"
    exit 1
fi

OUT_LOG=""
ERR_LOG=""
if [[ -f $CURRENT_RUN_PATH ]]; then
    PYTHON_EXE=$(find_python "$ROOT")
    readarray -t LOG_PATHS < <("$PYTHON_EXE" - "$CURRENT_RUN_PATH" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8-sig") as handle:
        run = json.load(handle)
    print(run.get("stdout", ""))
    print(run.get("stderr", ""))
except (OSError, ValueError):
    print("")
    print("")
PY
)
    OUT_LOG=${LOG_PATHS[0]:-}
    ERR_LOG=${LOG_PATHS[1]:-}
fi

echo "Zhuorui listener is running with PID $PID_VALUE."
[[ -z $OUT_LOG ]] || echo "stdout: $OUT_LOG"
[[ -z $ERR_LOG ]] || echo "stderr: $ERR_LOG"
if [[ -n $OUT_LOG && -f $OUT_LOG ]]; then
    echo
    echo "Recent stdout:"
    tail -n 10 -- "$OUT_LOG"
fi
if [[ -n $ERR_LOG && -f $ERR_LOG ]]; then
    echo
    echo "Recent stderr:"
    tail -n 10 -- "$ERR_LOG"
fi
