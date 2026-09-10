#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=zhuorui_shell_common.sh
source "$ROOT/zhuorui_shell_common.sh"
PID_PATH="$ROOT/zhuorui_listener.pid"
CURRENT_RUN_PATH="$ROOT/zhuorui_listener.current.json"
SCRIPT_PATH="$ROOT/zhuorui_market_order.py"
PYTHON_EXE=$(find_python "$ROOT")

if [[ ! -f $PID_PATH ]]; then
    echo "Zhuorui listener is not running: no PID file found."
    exit 0
fi
PID_VALUE=$(tr -d '[:space:]' < "$PID_PATH")
if ! valid_pid "$PID_VALUE"; then
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
    echo "Removed invalid PID file."
    exit 0
fi
if ! process_is_running "$PID_VALUE"; then
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
    echo "Zhuorui listener was not running. Removed stale PID file."
    exit 0
fi
if ! tracked_process_matches "$PID_VALUE" "$CURRENT_RUN_PATH" "$SCRIPT_PATH" "$PYTHON_EXE"; then
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
    echo "Listener PID file was stale; process $PID_VALUE was left untouched."
    exit 0
fi

readarray -t LOG_PATHS < <("$PYTHON_EXE" - "$CURRENT_RUN_PATH" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8-sig") as handle:
    run = json.load(handle)
print(run.get("stdout", ""))
print(run.get("stderr", ""))
PY
)
stop_process_group "$PID_VALUE" || { echo "Could not stop Zhuorui listener process $PID_VALUE." >&2; exit 1; }
rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
echo "stdout: ${LOG_PATHS[0]:-}"
echo "stderr: ${LOG_PATHS[1]:-}"
echo "Stopped Zhuorui listener with PID $PID_VALUE."
