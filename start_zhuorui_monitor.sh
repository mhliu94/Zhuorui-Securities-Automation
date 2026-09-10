#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=zhuorui_shell_common.sh
source "$ROOT/zhuorui_shell_common.sh"

PORT=443
REDIRECT_HTTP_PORT=80
HOST_ADDRESS=0.0.0.0
PUBLIC_HOST=""
INTERVAL=60
CERTIFICATE_PATH="$ROOT/certs/zhuorui-monitor-cert.pem"
PRIVATE_KEY_PATH="$ROOT/certs/zhuorui-monitor-key.pem"
OPEN_BROWSER=0

while (( $# )); do
    case $1 in
        --port) PORT=${2:?Missing value for $1}; shift 2 ;;
        --redirect-http-port) REDIRECT_HTTP_PORT=${2:?Missing value for $1}; shift 2 ;;
        --host-address|--host) HOST_ADDRESS=${2:?Missing value for $1}; shift 2 ;;
        --public-host) PUBLIC_HOST=${2:?Missing value for $1}; shift 2 ;;
        --interval) INTERVAL=${2:?Missing value for $1}; shift 2 ;;
        --certificate-path|--cert-file) CERTIFICATE_PATH=${2:?Missing value for $1}; shift 2 ;;
        --private-key-path|--key-file) PRIVATE_KEY_PATH=${2:?Missing value for $1}; shift 2 ;;
        --open-browser) OPEN_BROWSER=1; shift ;;
        --help|-h)
            echo "Usage: $0 [--port PORT] [--redirect-http-port PORT] [--host-address HOST] [--public-host HOST] [--interval SECONDS] [--certificate-path PATH] [--private-key-path PATH] [--open-browser]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
for VALUE in "$PORT" "$REDIRECT_HTTP_PORT" "$INTERVAL"; do
    [[ $VALUE =~ ^[0-9]+$ ]] || { echo "Port and interval values must be integers." >&2; exit 2; }
done
(( PORT >= 1 && PORT <= 65535 )) || { echo "Port must be between 1 and 65535." >&2; exit 2; }
(( REDIRECT_HTTP_PORT >= 1 && REDIRECT_HTTP_PORT <= 65535 && REDIRECT_HTTP_PORT != PORT )) || {
    echo "Redirect HTTP port must be between 1 and 65535 and different from the HTTPS port." >&2
    exit 2
}
(( INTERVAL >= 10 && INTERVAL <= 86400 )) || { echo "Interval must be between 10 and 86400 seconds." >&2; exit 2; }

PYTHON_EXE=$(find_python "$ROOT")
command -v setsid >/dev/null 2>&1 || { echo "The util-linux 'setsid' command is required." >&2; exit 1; }
command -v setsid >/dev/null 2>&1 || { echo "The util-linux 'setsid' command is required." >&2; exit 1; }
if [[ -z $PUBLIC_HOST ]]; then
    PUBLIC_HOST=$("$PYTHON_EXE" - <<'PY'
import socket
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("8.8.8.8", 443))
        print(probe.getsockname()[0])
except OSError:
    pass
PY
)
fi
if [[ -z $PUBLIC_HOST && -f $ROOT/zhuorui_config.json ]]; then
    PUBLIC_HOST=$("$PYTHON_EXE" - "$ROOT/zhuorui_config.json" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8-sig") as handle:
        value = json.load(handle).get("public_host", "")
    print(value if isinstance(value, str) else "")
except (OSError, ValueError):
    pass
PY
)
fi
PUBLIC_HOST=${PUBLIC_HOST:-localhost}

if [[ ! -f $CERTIFICATE_PATH || ! -f $PRIVATE_KEY_PATH ]]; then
    if [[ $CERTIFICATE_PATH == "$ROOT/certs/zhuorui-monitor-cert.pem" && $PRIVATE_KEY_PATH == "$ROOT/certs/zhuorui-monitor-key.pem" ]]; then
        "$ROOT/setup_zhuorui_monitor_https.sh"
    else
        echo "The requested certificate or private key does not exist." >&2
        exit 1
    fi
fi

SERVER_PATH="$ROOT/zhuorui_monitor.py"
PID_PATH="$ROOT/zhuorui_monitor.pid"
CURRENT_RUN_PATH="$ROOT/zhuorui_monitor.current.json"
LOG_DIR="$ROOT/logs"
URL_HOST=$HOST_ADDRESS
[[ $URL_HOST != 0.0.0.0 && $URL_HOST != :: ]] || URL_HOST=localhost
[[ $URL_HOST != *:* ]] || URL_HOST="[$URL_HOST]"
[[ $PUBLIC_HOST != *:* ]] || EXTERNAL_HOST="[$PUBLIC_HOST]"
EXTERNAL_HOST=${EXTERNAL_HOST:-$PUBLIC_HOST}
URL="https://$URL_HOST"
EXTERNAL_URL="https://$EXTERNAL_HOST"
[[ $PORT == 443 ]] || { URL="$URL:$PORT"; EXTERNAL_URL="$EXTERNAL_URL:$PORT"; }
URL="$URL/"
EXTERNAL_URL="$EXTERNAL_URL/"

if [[ -f $PID_PATH ]]; then
    EXISTING_PID=$(tr -d '[:space:]' < "$PID_PATH")
    if valid_pid "$EXISTING_PID" && tracked_process_matches "$EXISTING_PID" "$CURRENT_RUN_PATH" "$SERVER_PATH" "$PYTHON_EXE"; then
        echo "Zhuorui Control Room is already running with PID $EXISTING_PID."
        echo "$URL"
        if (( OPEN_BROWSER )); then xdg-open "$URL" >/dev/null 2>&1 || true; fi
        exit 0
    fi
    if valid_pid "$EXISTING_PID" && process_is_running "$EXISTING_PID"; then
        echo "Monitor PID file was stale; process $EXISTING_PID was left untouched."
    fi
    rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
fi

mkdir -p -- "$LOG_DIR"
RUN_STARTED_UTC=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
RUN_STAMP=$(date -u +'%Y%m%dT%H%M%SZ')
OUT_LOG="$LOG_DIR/zhuorui_monitor_$RUN_STAMP.out.log"
ERR_LOG="$LOG_DIR/zhuorui_monitor_$RUN_STAMP.err.log"
(
    cd -- "$ROOT"
    exec setsid "$PYTHON_EXE" "$SERVER_PATH" \
        --host "$HOST_ADDRESS" --port "$PORT" \
        --redirect-http-port "$REDIRECT_HTTP_PORT" --public-host "$PUBLIC_HOST" \
        --interval "$INTERVAL" --cert-file "$CERTIFICATE_PATH" --key-file "$PRIVATE_KEY_PATH"
) </dev/null >>"$OUT_LOG" 2>>"$ERR_LOG" &
MONITOR_PID=$!
printf '%s\n' "$MONITOR_PID" > "$PID_PATH"
"$PYTHON_EXE" - "$CURRENT_RUN_PATH" "$MONITOR_PID" "$RUN_STARTED_UTC" "$URL" "$EXTERNAL_URL" "$OUT_LOG" "$ERR_LOG" <<'PY'
import json, sys
path, pid, started, url, external_url, stdout, stderr = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "pid": int(pid), "started_utc": started, "url": url,
        "external_url": external_url, "stdout": stdout, "stderr": stderr,
    }, handle, indent=2)
PY

READY=0
for _ in {1..20}; do
    sleep 0.25
    process_is_running "$MONITOR_PID" || break
    if "$PYTHON_EXE" - "$URL" <<'PY' >/dev/null 2>&1
import ssl, sys, urllib.request
urllib.request.urlopen(sys.argv[1] + "healthz", context=ssl._create_unverified_context(), timeout=1).read()
PY
    then
        READY=1
        break
    fi
done
if (( ! READY )); then
    if ! process_is_running "$MONITOR_PID"; then
        rm -f -- "$PID_PATH" "$CURRENT_RUN_PATH"
        DETAILS=$(tail -n 20 -- "$ERR_LOG" 2>/dev/null || true)
        echo "Zhuorui Control Room exited during startup. $DETAILS" >&2
        exit 1
    fi
    echo "WARNING: The process started, but the dashboard did not answer within five seconds. Check $ERR_LOG" >&2
fi

echo "Started Zhuorui Control Room with PID $MONITOR_PID."
echo "$URL"
if (( OPEN_BROWSER )); then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || echo "WARNING: Could not open the browser." >&2
    else
        echo "WARNING: xdg-open is not installed; open $URL manually." >&2
    fi
fi
