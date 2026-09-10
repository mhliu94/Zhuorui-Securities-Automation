#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=zhuorui_shell_common.sh
source "$ROOT/zhuorui_shell_common.sh"

CONFIG_PATH="$ROOT/zhuorui_config.json"
while (( $# )); do
    case $1 in
        --config|-c)
            [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
            CONFIG_PATH=$2
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--config PATH]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done
if [[ $CONFIG_PATH != /* ]]; then
    CONFIG_PATH="$ROOT/$CONFIG_PATH"
fi

SCRIPT_PATH="$ROOT/zhuorui_market_order.py"
PID_PATH="$ROOT/zhuorui_listener.pid"
CURRENT_RUN_PATH="$ROOT/zhuorui_listener.current.json"
LOG_DIR="$ROOT/logs"
PYTHON_EXE=$(find_python "$ROOT")
command -v setsid >/dev/null 2>&1 || { echo "The util-linux 'setsid' command is required." >&2; exit 1; }
command -v setsid >/dev/null 2>&1 || { echo "The util-linux 'setsid' command is required." >&2; exit 1; }

if [[ -f $PID_PATH ]]; then
    EXISTING_PID=$(tr -d '[:space:]' < "$PID_PATH")
    if valid_pid "$EXISTING_PID" && tracked_process_matches "$EXISTING_PID" "$CURRENT_RUN_PATH" "$SCRIPT_PATH" "$PYTHON_EXE"; then
        echo "Zhuorui listener is already running with PID $EXISTING_PID."
        "$PYTHON_EXE" - "$CURRENT_RUN_PATH" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8-sig") as handle:
    run = json.load(handle)
print(f"stdout: {run.get('stdout', '')}")
print(f"stderr: {run.get('stderr', '')}")
PY
        exit 0
    fi
    if valid_pid "$EXISTING_PID" && process_is_running "$EXISTING_PID"; then
        echo "Listener PID file was stale; process $EXISTING_PID was left untouched."
    fi
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
fi

mkdir -p -- "$LOG_DIR"
RUN_STARTED_UTC=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
RUN_STAMP=$(date -u +'%Y%m%dT%H%M%SZ')
OUT_LOG="$LOG_DIR/zhuorui_listener_$RUN_STAMP.out.log"
ERR_LOG="$LOG_DIR/zhuorui_listener_$RUN_STAMP.err.log"

(
    cd -- "$ROOT"
    exec setsid "$PYTHON_EXE" "$SCRIPT_PATH" server --config "$CONFIG_PATH"
) </dev/null >>"$OUT_LOG" 2>>"$ERR_LOG" &
LISTENER_PID=$!

printf '%s\n' "$LISTENER_PID" > "$PID_PATH"
"$PYTHON_EXE" - "$CURRENT_RUN_PATH" "$LISTENER_PID" "$RUN_STARTED_UTC" "$OUT_LOG" "$ERR_LOG" "$CONFIG_PATH" <<'PY'
import json, sys
path, pid, started, stdout, stderr, config = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "pid": int(pid), "started_utc": started, "stdout": stdout,
        "stderr": stderr, "config": config,
    }, handle, indent=2)
PY

sleep 0.2
if ! process_is_running "$LISTENER_PID"; then
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
    echo "Zhuorui listener exited during startup. See $ERR_LOG" >&2
    exit 1
fi
echo "Started Zhuorui listener with PID $LISTENER_PID."
echo "stdout: $OUT_LOG"
echo "stderr: $ERR_LOG"
