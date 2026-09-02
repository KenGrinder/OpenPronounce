#!/bin/sh
# Start uvicorn, optionally terminating TLS itself.
#
# Browsers only expose navigator.mediaDevices in a secure context, so the record
# button in the web UI is dead over plain http:// to a LAN address. A real
# certificate from a reverse proxy is the right answer in production; this exists
# so a self-hosted box on a trusted LAN can get a working microphone without one.
set -eu

PORT="${PORT:-8000}"
CERT_DIR="${OPENPRONOUNCE_SSL_DIR:-/config/certs}"
CRT="$CERT_DIR/server.crt"
KEY="$CERT_DIR/server.key"
SSL_MODE="${OPENPRONOUNCE_SSL:-off}"

if [ "$SSL_MODE" = "selfsigned" ] && { [ ! -s "$CRT" ] || [ ! -s "$KEY" ]; }; then
    mkdir -p "$CERT_DIR"

    # Chrome rejects a certificate with no subjectAltName outright -- a CN alone
    # is not enough to reach the "proceed anyway" path. Every address the browser
    # might use has to be named here, so the cert has to know the LAN IP.
    SAN="DNS:localhost,IP:127.0.0.1"
    for host in $(echo "${OPENPRONOUNCE_SSL_HOSTS:-}" | tr ',' ' '); do
        [ -n "$host" ] || continue
        case "$host" in
            *[!0-9.]*) SAN="$SAN,DNS:$host" ;;
            *)         SAN="$SAN,IP:$host" ;;
        esac
    done

    echo "openpronounce: generating self-signed certificate for $SAN"
    # 825 days: the longest validity Chrome accepts for a manually trusted
    # certificate. Longer and it is rejected before you can add the exception.
    #
    # stderr is captured rather than discarded. openssl is noisy on success (a
    # line of progress dots), but on failure its message is the only clue the
    # operator gets, and swallowing it surfaces later as the far more confusing
    # "certificate missing" error below.
    if ! err="$(openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
        -keyout "$KEY" -out "$CRT" \
        -subj "/CN=openpronounce" -addext "subjectAltName=$SAN" 2>&1)"; then
        echo "openpronounce: certificate generation failed" >&2
        echo "$err" >&2
        exit 1
    fi
    chmod 600 "$KEY"
fi

if [ "$SSL_MODE" != "off" ]; then
    if [ ! -s "$CRT" ] || [ ! -s "$KEY" ]; then
        echo "openpronounce: OPENPRONOUNCE_SSL=$SSL_MODE but $CRT / $KEY are missing" >&2
        exit 1
    fi
    echo "openpronounce: serving https on :$PORT"
    set -- --ssl-keyfile "$KEY" --ssl-certfile "$CRT"
else
    set --
fi

exec uvicorn server:app \
    --host 0.0.0.0 --port "$PORT" --workers 1 \
    --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    "$@"
