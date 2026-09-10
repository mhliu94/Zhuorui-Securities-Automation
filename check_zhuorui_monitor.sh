#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=zhuorui_shell_common.sh
source "$ROOT/zhuorui_shell_common.sh"
PORT=443
HOST_ADDRESS=localhost
while (( $# )); do
    case $1 in
        --port) PORT=${2:?Missing value for $1}; shift 2 ;;
        --host-address|--host) HOST_ADDRESS=${2:?Missing value for $1}; shift 2 ;;
        --help|-h) echo "Usage: $0 [--port PORT] [--host-address HOST]"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ $PORT =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || { echo "Port must be between 1 and 65535." >&2; exit 2; }
URL_HOST=$HOST_ADDRESS
[[ $URL_HOST != *:* ]] || URL_HOST="[$URL_HOST]"
URL="https://$URL_HOST"
[[ $PORT == 443 ]] || URL="$URL:$PORT"
URL="$URL/healthz"
PYTHON_EXE=$(find_python "$ROOT")
if ! "$PYTHON_EXE" - "$URL" <<'PY' >/dev/null
import ssl, sys, urllib.request
print(urllib.request.urlopen(sys.argv[1], context=ssl._create_unverified_context(), timeout=5).read().decode())
PY
then
    echo "Zhuorui Control Room is not responding at $URL"
    exit 1
fi
echo "Authenticated Zhuorui Control Room is responding over HTTPS."
