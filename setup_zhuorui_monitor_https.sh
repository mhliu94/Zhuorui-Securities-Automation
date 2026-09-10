#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=zhuorui_shell_common.sh
source "$ROOT/zhuorui_shell_common.sh"
PYTHON_EXE=$(find_python "$ROOT")
COMMON_NAME=$(hostname -f 2>/dev/null || hostname)
FORCE=0
ADDITIONAL_DNS=()
ADDITIONAL_IPS=()

while (( $# )); do
    case $1 in
        --common-name)
            [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
            COMMON_NAME=$2; shift 2
            ;;
        --additional-dns-name)
            [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
            ADDITIONAL_DNS+=("$2"); shift 2
            ;;
        --additional-ip-address)
            [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
            ADDITIONAL_IPS+=("$2"); shift 2
            ;;
        --force|-f) FORCE=1; shift ;;
        --help|-h)
            echo "Usage: $0 [--common-name NAME] [--additional-dns-name NAME] [--additional-ip-address IP] [--force]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

command -v openssl >/dev/null 2>&1 || { echo "OpenSSL was not found. Install the openssl package." >&2; exit 1; }
CERT_DIR="$ROOT/certs"
CERT_PATH="$CERT_DIR/zhuorui-monitor-cert.pem"
CERT_DER_PATH="$CERT_DIR/zhuorui-monitor-cert.cer"
KEY_PATH="$CERT_DIR/zhuorui-monitor-key.pem"

if [[ -f $CERT_PATH && -f $KEY_PATH && $FORCE -eq 0 ]]; then
    echo "HTTPS certificate already exists."
    echo "Certificate: $CERT_PATH"
    echo "Private key: $KEY_PATH"
    exit 0
fi
if (( FORCE )); then
    rm -f -- "$CERT_PATH" "$CERT_DER_PATH" "$KEY_PATH"
fi
mkdir -p -- "$CERT_DIR"

SAFE_COMMON_NAME=${COMMON_NAME//[^A-Za-z0-9._-]/-}
DNS_NAMES=("$SAFE_COMMON_NAME" localhost "${ADDITIONAL_DNS[@]}")
IP_ADDRESSES=(127.0.0.1 "${ADDITIONAL_IPS[@]}")
if command -v hostname >/dev/null 2>&1; then
    read -r -a HOST_IPS <<< "$(hostname -I 2>/dev/null || true)"
    IP_ADDRESSES+=("${HOST_IPS[@]}")
fi

OPENSSL_CONFIG=$(mktemp "${TMPDIR:-/tmp}/zhuorui-openssl.XXXXXX")
cleanup() { rm -f -- "$OPENSSL_CONFIG"; }
trap cleanup EXIT

SAN_ENTRIES=$("$PYTHON_EXE" - "$SAFE_COMMON_NAME" "${DNS_NAMES[*]}" "${IP_ADDRESSES[*]}" <<'PY'
import ipaddress
import re
import sys

dns_values = []
for value in sys.argv[2].split():
    safe = re.sub(r"[^A-Za-z0-9.*_-]", "-", value)
    if safe and safe not in dns_values:
        dns_values.append(safe)
ip_values = []
for value in sys.argv[3].split():
    try:
        parsed = str(ipaddress.ip_address(value))
    except ValueError:
        continue
    if not parsed.startswith("169.254.") and parsed not in ip_values:
        ip_values.append(parsed)
print(",".join([*(f"DNS:{value}" for value in dns_values), *(f"IP:{value}" for value in ip_values)]))
PY
)

{
    echo '[req]'
    echo 'prompt = no'
    echo 'distinguished_name = subject'
    echo 'x509_extensions = server_extensions'
    echo
    echo '[subject]'
    echo "CN = $SAFE_COMMON_NAME"
    echo
    echo '[server_extensions]'
    echo "subjectAltName = $SAN_ENTRIES"
    echo 'basicConstraints = critical,CA:FALSE'
    echo 'keyUsage = critical,digitalSignature,keyEncipherment'
    echo 'extendedKeyUsage = serverAuth'
    echo 'subjectKeyIdentifier = hash'
    echo 'authorityKeyIdentifier = keyid,issuer'
} > "$OPENSSL_CONFIG"

openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 397 \
    -keyout "$KEY_PATH" -out "$CERT_PATH" -config "$OPENSSL_CONFIG"
openssl x509 -in "$CERT_PATH" -outform der -out "$CERT_DER_PATH"
chmod 600 -- "$KEY_PATH"
chmod 644 -- "$CERT_PATH" "$CERT_DER_PATH"

echo "Created a self-signed HTTPS certificate for:"
tr ',' '\n' <<< "$SAN_ENTRIES" | sed 's/^/  /'
echo "Certificate: $CERT_PATH"
echo "DER certificate: $CERT_DER_PATH"
echo "Private key: $KEY_PATH"
echo "WARNING: Browsers will show a trust warning until this certificate is trusted or replaced with one from a public certificate authority." >&2
