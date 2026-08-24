#!/usr/bin/env bash

set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
installer="$repo_root/scripts/install-control-center-tunnel-user.sh"
verifier="$repo_root/scripts/verify-control-center-loopback.sh"
deploy_helper="$repo_root/scripts/deploy-telegram-bot.sh"
override="$repo_root/deploy/compose.control-center-loopback.yml"
docs="$repo_root/docs/CONTROL_CENTER_PRIVATE_TUNNEL.md"

bash -n "$installer"
bash -n "$verifier"
bash -n "$deploy_helper"

for path in "$installer" "$verifier" "$deploy_helper" "$override" "$docs"; do
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
    "$repo_root/scripts/verify-control-center-loopback.sh" "$deploy_helper" "$docs"; then
    printf '%s\n' 'private tunnel files contain a credential-like value' >&2
    exit 1
fi

for required in \
    'compose.control-center-loopback.yml' \
    '--no-deps telegram-bot' \
    'verify-control-center-loopback.sh'; do
    grep -Fq -- "$required" "$deploy_helper" || {
        printf 'missing canonical deploy contract: %s\n' "$required" >&2
        exit 1
    }
done

if grep -Eq '0\.0\.0\.0|\[::\]' "$deploy_helper"; then
    printf '%s\n' 'canonical deploy helper must not introduce public bindings' >&2
    exit 1
fi

if grep -Fq 'docker compose up -d --build telegram-bot' "$repo_root/README.md"; then
    printf '%s\n' 'README still documents the parent-only telegram-bot deploy command' >&2
    exit 1
fi

temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/alice-deploy-contract.XXXXXX")"
temporary_root="$(cd "$temporary_root" && pwd -P)"
trap 'rm -rf "$temporary_root"' EXIT

: > "$temporary_root/compose.yml"
dry_run_output="$($deploy_helper --parent-root "$temporary_root" --dry-run)"
grep -Fq "parent_root=$temporary_root" <<<"$dry_run_output"
grep -Fq "compose_file=$temporary_root/compose.yml" <<<"$dry_run_output"
grep -Fq "override_file=$override" <<<"$dry_run_output"
grep -Fq 'service=telegram-bot' <<<"$dry_run_output"
grep -Fq "verifier=$verifier" <<<"$dry_run_output"

: > "$temporary_root/docker-compose.yml"
if "$deploy_helper" --parent-root "$temporary_root" --dry-run >/dev/null 2>&1; then
    printf '%s\n' 'ambiguous parent Compose files must fail closed' >&2
    exit 1
fi

explicit_output="$($deploy_helper --parent-root "$temporary_root" --compose-file docker-compose.yml --dry-run)"
grep -Fq "compose_file=$temporary_root/docker-compose.yml" <<<"$explicit_output"

if "$deploy_helper" --parent-root "$temporary_root/missing" --dry-run >/dev/null 2>&1; then
    printf '%s\n' 'missing parent Compose directory must fail closed' >&2
    exit 1
fi

printf '%s\n' 'Private tunnel deployment contracts passed.'
