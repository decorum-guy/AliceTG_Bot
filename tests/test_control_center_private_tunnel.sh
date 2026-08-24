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

retry_root="$temporary_root/retry-harness"
mkdir -p "$retry_root/bin" "$retry_root/alice/scripts" "$retry_root/alice/deploy" "$retry_root/parent"
cp "$repo_root/scripts/deploy-telegram-bot.sh" "$retry_root/alice/scripts/deploy-telegram-bot.sh"
cp "$repo_root/deploy/compose.control-center-loopback.yml" "$retry_root/alice/deploy/compose.control-center-loopback.yml"
: > "$retry_root/parent/compose.yml"

cat > "$retry_root/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >> "$HARNESS_DOCKER_LOG"
if [[ "$1" == "compose" && "$2" == "version" ]]; then
    exit 0
fi
if [[ "$1" == "compose" && "$*" == *" up "* ]]; then
    printf '%s\n' up >> "$HARNESS_UP_LOG"
    exit 0
fi
exit 64
EOF
chmod +x "$retry_root/bin/docker"

cat > "$retry_root/bin/sleep" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >> "$HARNESS_SLEEP_LOG"
EOF
chmod +x "$retry_root/bin/sleep"

cat > "$retry_root/bin/date" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$1" != "+%s" ]]; then
    exec /bin/date "$@"
fi
count=0
if [[ -f "$HARNESS_DATE_STATE" ]]; then
    count="$(<"$HARNESS_DATE_STATE")"
fi
count=$((count + 1))
printf '%s\n' "$count" > "$HARNESS_DATE_STATE"
printf '%s\n' "$((100 + count))"
EOF
chmod +x "$retry_root/bin/date"

cat > "$retry_root/alice/scripts/verify-control-center-loopback.sh" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
count=0
if [[ -f "$HARNESS_VERIFIER_STATE" ]]; then
    count="$(<"$HARNESS_VERIFIER_STATE")"
fi
count=$((count + 1))
printf '%s\n' "$count" > "$HARNESS_VERIFIER_STATE"
printf '%s\n' "$count" >> "$HARNESS_VERIFIER_LOG"
case "$HARNESS_VERIFIER_MODE" in
    transient)
        if (( count < 3 )); then
            exit 69
        fi
        exit 0
        ;;
    persistent)
        exit 69
        ;;
    security)
        exit 77
        ;;
    configuration)
        exit 64
        ;;
    *)
        exit 64
        ;;
esac
EOF
chmod +x "$retry_root/alice/scripts/verify-control-center-loopback.sh"

run_retry_case() {
    local mode="$1"
    local expected_rc="$2"
    local case_dir="$retry_root/$mode"
    local actual_rc
    mkdir -p "$case_dir"
    : > "$case_dir/docker.log"
    : > "$case_dir/up.log"
    : > "$case_dir/sleep.log"
    : > "$case_dir/verifier.log"
    : > "$case_dir/date.state"
    : > "$case_dir/verifier.state"
    set +e
    (
        export PATH="$retry_root/bin:$PATH"
        export HARNESS_DOCKER_LOG="$case_dir/docker.log"
        export HARNESS_UP_LOG="$case_dir/up.log"
        export HARNESS_SLEEP_LOG="$case_dir/sleep.log"
        export HARNESS_VERIFIER_LOG="$case_dir/verifier.log"
        export HARNESS_DATE_STATE="$case_dir/date.state"
        export HARNESS_VERIFIER_STATE="$case_dir/verifier.state"
        export HARNESS_VERIFIER_MODE="$mode"
        "$retry_root/alice/scripts/deploy-telegram-bot.sh" --parent-root "$retry_root/parent"
    )
    actual_rc=$?
    set -e
    [[ "$actual_rc" == "$expected_rc" ]] || {
        printf 'retry case %s returned %s, expected %s\n' "$mode" "$actual_rc" "$expected_rc" >&2
        exit 1
    }
    [[ "$(wc -l < "$case_dir/up.log" | tr -d ' ')" == 1 ]] || {
        printf 'retry case %s recreated more than once\n' "$mode" >&2
        exit 1
    }
}

run_retry_case transient 0
[[ "$(wc -l < "$retry_root/transient/verifier.log" | tr -d ' ')" == 3 ]]
[[ "$(wc -l < "$retry_root/transient/sleep.log" | tr -d ' ')" == 2 ]] || {
    printf '%s\n' 'transient retry interval was not exercised twice' >&2
    exit 1
}

run_retry_case persistent 69
persistent_attempts="$(wc -l < "$retry_root/persistent/verifier.log" | tr -d ' ')"
[[ "$persistent_attempts" -ge 2 && "$persistent_attempts" -le 31 ]] || {
    printf 'persistent retry count out of bounds: %s\n' "$persistent_attempts" >&2
    exit 1
}
persistent_sleeps="$(wc -l < "$retry_root/persistent/sleep.log" | tr -d ' ')"
[[ "$persistent_sleeps" -ge 1 && "$persistent_sleeps" -le 30 ]] || {
    printf 'persistent sleep count out of bounds: %s\n' "$persistent_sleeps" >&2
    exit 1
}

run_retry_case security 77
[[ "$(wc -l < "$retry_root/security/verifier.log" | tr -d ' ')" == 1 ]]
[[ ! -s "$retry_root/security/sleep.log" ]]

run_retry_case configuration 64
[[ "$(wc -l < "$retry_root/configuration/verifier.log" | tr -d ' ')" == 1 ]]
[[ ! -s "$retry_root/configuration/sleep.log" ]]

printf '%s\n' 'Private tunnel deployment contracts passed.'
