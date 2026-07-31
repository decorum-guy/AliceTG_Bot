#!/usr/bin/env bash

set -Eeuo pipefail

readonly HA_PORT="${CONTROL_CENTER_HA_LOOPBACK_PORT:-18123}"
readonly BOT_PORT="${CONTROL_CENTER_BOT_LOOPBACK_PORT:-18088}"

fail() {
    printf 'error: %s\n' "$1" >&2
    exit "${2:-1}"
}

for port in "$HA_PORT" "$BOT_PORT"; do
    [[ "$port" =~ ^[0-9]+$ ]] || fail "invalid port" 64
    (( port >= 1 && port <= 65535 )) || fail "port out of range" 64
done

listeners="$(ss -H -ltn 2>/dev/null || true)"
for port in "$HA_PORT" "$BOT_PORT"; do
    printf '%s\n' "$listeners" | grep -Eq "127\.0\.0\.1:${port}([[:space:]]|$)" \
        || fail "127.0.0.1:$port is not listening" 69
    if printf '%s\n' "$listeners" | grep -Eq "(^|[[:space:]])(0\.0\.0\.0|\[::\]|\*):${port}([[:space:]]|$)"; then
        fail "port $port is exposed beyond loopback" 77
    fi
done

ha_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time 10 "http://127.0.0.1:$HA_PORT/api/")"
[[ "$ha_status" == "401" || "$ha_status" == "200" ]] \
    || fail "Home Assistant loopback endpoint returned HTTP $ha_status" 69

bot_live_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time 10 "http://127.0.0.1:$BOT_PORT/health/live")"
[[ "$bot_live_status" == "200" ]] \
    || fail "AliceTG live endpoint returned HTTP $bot_live_status" 69

bot_internal_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time 10 "http://127.0.0.1:$BOT_PORT/internal/control-center/coffee/timing")"
[[ "$bot_internal_status" == "401" || "$bot_internal_status" == "503" ]] \
    || fail "AliceTG internal API did not reject an unauthenticated request" 77

printf 'Loopback exposure verified.\n'
printf 'Home Assistant: 127.0.0.1:%s\n' "$HA_PORT"
printf 'AliceTG Bot: 127.0.0.1:%s\n' "$BOT_PORT"
printf 'No token or internal response body was printed.\n'
