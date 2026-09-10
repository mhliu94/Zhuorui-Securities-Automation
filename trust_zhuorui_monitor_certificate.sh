#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CERTIFICATE_PATH="$ROOT/certs/zhuorui-monitor-cert.pem"
STORE_SCOPE=system
while (( $# )); do
    case $1 in
        --certificate-path) CERTIFICATE_PATH=${2:?Missing value for $1}; shift 2 ;;
        --store-scope) STORE_SCOPE=${2:?Missing value for $1}; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--certificate-path PATH] [--store-scope system|user]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -f $CERTIFICATE_PATH ]] || {
    echo "Certificate file not found. Run setup_zhuorui_monitor_https.sh first." >&2
    exit 1
}
case $STORE_SCOPE in
    user)
        command -v trust >/dev/null 2>&1 || {
            echo "The p11-kit 'trust' command is required for a per-user trust anchor." >&2
            exit 1
        }
        trust anchor "$CERTIFICATE_PATH"
        echo "Trusted the Zhuorui HTTPS certificate for the current user."
        ;;
    system)
        (( EUID == 0 )) || { echo "Run this script as root (for example, with sudo) for system trust." >&2; exit 1; }
        if command -v update-ca-certificates >/dev/null 2>&1; then
            install -m 0644 "$CERTIFICATE_PATH" /usr/local/share/ca-certificates/zhuorui-monitor.crt
            update-ca-certificates
        elif command -v update-ca-trust >/dev/null 2>&1; then
            install -m 0644 "$CERTIFICATE_PATH" /etc/pki/ca-trust/source/anchors/zhuorui-monitor.pem
            update-ca-trust extract
        elif command -v trust >/dev/null 2>&1; then
            trust anchor "$CERTIFICATE_PATH"
        else
            echo "No supported Linux CA trust-store tool was found." >&2
            exit 1
        fi
        echo "Trusted the Zhuorui HTTPS certificate in the system root store."
        ;;
    *) echo "Store scope must be 'system' or 'user'." >&2; exit 2 ;;
esac
openssl x509 -in "$CERTIFICATE_PATH" -noout -fingerprint -sha256
echo "Restart open browsers so they reload the trust store."
