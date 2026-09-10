#!/usr/bin/env bash
set -euo pipefail

HTTPS_PORT=443
HTTP_REDIRECT_PORT=80
while (( $# )); do
    case $1 in
        --https-port) HTTPS_PORT=${2:?Missing value for $1}; shift 2 ;;
        --http-redirect-port) HTTP_REDIRECT_PORT=${2:?Missing value for $1}; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--https-port PORT] [--http-redirect-port PORT]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
for PORT in "$HTTPS_PORT" "$HTTP_REDIRECT_PORT"; do
    [[ $PORT =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || {
        echo "Ports must be integers between 1 and 65535." >&2
        exit 2
    }
done
(( HTTPS_PORT != HTTP_REDIRECT_PORT )) || { echo "HTTPS and redirect ports must differ." >&2; exit 2; }
(( EUID == 0 )) || { echo "Run this script as root (for example, with sudo)." >&2; exit 1; }

if command -v ufw >/dev/null 2>&1; then
    ufw allow "$HTTP_REDIRECT_PORT/tcp" comment 'Zhuorui Control Room HTTP redirect'
    ufw allow "$HTTPS_PORT/tcp" comment 'Zhuorui Control Room HTTPS'
    echo "Allowed TCP ports $HTTP_REDIRECT_PORT and $HTTPS_PORT with UFW."
elif command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$HTTP_REDIRECT_PORT/tcp"
    firewall-cmd --permanent --add-port="$HTTPS_PORT/tcp"
    firewall-cmd --reload
    echo "Allowed TCP ports $HTTP_REDIRECT_PORT and $HTTPS_PORT with firewalld."
else
    echo "Neither ufw nor firewall-cmd is installed. Add persistent TCP allow rules for ports $HTTP_REDIRECT_PORT and $HTTPS_PORT with the firewall manager used by this host." >&2
    exit 1
fi
