#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=zhuorui_shell_common.sh
source "$ROOT/zhuorui_shell_common.sh"
PID_PATH="$ROOT/zhuorui_monitor.pid"
CURRENT_RUN_PATH="$ROOT/zhuorui_monitor.current.json"
SERVER_PATH="$ROOT/zhuorui_monitor.py"
PYTHON_EXE=$(find_python "$ROOT")

if [[ ! -f $PID_PATH ]]; then
    echo "Zhuorui Control Room is not running: no PID file found."
    exit 0
fi
PID_VALUE=$(tr -d '[:space:]' < "$PID_PATH")
if ! valid_pid "$PID_VALUE"; then
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
    echo "Removed invalid monitor PID file."
    exit 0
fi
if ! process_is_running "$PID_VALUE"; then
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
    echo "Zhuorui Control Room was not running. Removed stale PID file."
    exit 0
fi
if ! tracked_process_matches "$PID_VALUE" "$CURRENT_RUN_PATH" "$SERVER_PATH" "$PYTHON_EXE"; then
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
    echo "Monitor PID file was stale; process $PID_VALUE was left untouched."
    exit 0
fi
stop_process_group "$PID_VALUE" || { echo "Could not stop Zhuorui Control Room process $PID_VALUE." >&2; exit 1; }
rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
echo "Stopped Zhuorui Control Room with PID $PID_VALUE."
