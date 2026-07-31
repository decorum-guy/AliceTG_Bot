#!/usr/bin/env bash

set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
installer="$repo_root/scripts/install-control-center-tunnel-user.sh"
verifier="$repo_root/scripts/verify-control-center-loopback.sh"
override="$repo_root/deploy/compose.control-center-loopback.yml"
docs="$repo_root/docs/CONTROL_CENTER_PRIVATE_TUNNEL.md"

bash -n "$installer"
bash -n "$verifier"

for path in "$installer" "$verifier" "$override" "$docs"; do
    [[ -f "$path" ]] || { printf 'missing file: %s\n' "$path" >&2; exit 1; }
done

for required in \
    'AllowTcpForwarding local' \
    'PermitOpen 127.0.0.1:$HA_PORT 127.0.0.1:$BOT_PORT' \
    'GatewayPorts no' \
    'AllowAgentForwarding no' \
    'X11Forwarding no' \
    'PermitTTY no' \
    'PermitTunnel no' \
    'MaxSessions 0' \
    'AuthenticationMethods publickey' \
    'sshd -t'; do
    grep -Fq "$required" "$installer" || {
        printf 'missing restricted sshd contract: %s\n' "$required" >&2
        exit 1
    }
done

grep -Fq 'restrict,port-forwarding' "$installer"
grep -Fq 'permitopen=\"127.0.0.1:$HA_PORT\"' "$installer"
grep -Fq 'permitopen=\"127.0.0.1:$BOT_PORT\"' "$installer"

for required in \
    '127.0.0.1:${CONTROL_CENTER_HA_LOOPBACK_PORT:-18123}:8123' \
    '127.0.0.1:${CONTROL_CENTER_BOT_LOOPBACK_PORT:-18088}:8088'; do
    grep -Fq "$required" "$override" || {
        printf 'missing loopback compose binding: %s\n' "$required" >&2
        exit 1
    }
done

if grep -Eq '0\.0\.0\.0:|\[::\]:' "$override"; then
    printf '%s\n' 'compose override must never bind Control Center ports publicly' >&2
    exit 1
fi

for required in \
    '127\.0\.0\.1:${port}' \
    '(0\.0\.0\.0|\[::\]|\*):${port}' \
    '/health/live' \
    '/internal/control-center/coffee/timing'; do
    grep -Fq "$required" "$verifier" || {
        printf 'missing loopback verifier contract: %s\n' "$required" >&2
        exit 1
    }
done

if grep -RInE '(BEGIN (RSA|OPENSSH) PRIVATE KEY|CONTROL_CENTER_API_TOKEN=[^[:space:]]+|HA_LONG_LIVED_TOKEN=[^[:space:]]+)' \
    "$repo_root/deploy" "$repo_root/scripts/install-control-center-tunnel-user.sh" \
    "$repo_root/scripts/verify-control-center-loopback.sh" "$docs"; then
    printf '%s\n' 'private tunnel files contain a credential-like value' >&2
    exit 1
fi

printf '%s\n' 'Private tunnel deployment contracts passed.'
