#!/usr/bin/env bash

set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
override_file="$repo_root/deploy/compose.control-center-loopback.yml"
verifier="$repo_root/scripts/verify-control-center-loopback.sh"
parent_root="$(cd "$repo_root/.." && pwd -P)"
compose_file=""
parent_root_explicit=false
dry_run=false

fail() {
    printf 'error: %s\n' "$1" >&2
    exit "${2:-1}"
}

usage() {
    cat <<'EOF'
Usage: deploy-telegram-bot.sh [--parent-root DIR] [--compose-file FILE] [--dry-run]

Rebuilds only telegram-bot with the Control Center loopback Compose override
and verifies the private listeners after deployment.
EOF
}

resolve_directory() {
    local directory="$1"
    [[ -d "$directory" ]] || fail "parent Compose directory does not exist: $directory" 64
    cd "$directory" && pwd -P
}

resolve_file() {
    local file="$1"
    local directory
    [[ -f "$file" ]] || fail "parent Compose file does not exist: $file" 64
    directory="$(cd "$(dirname "$file")" && pwd -P)" || fail "cannot resolve parent Compose file: $file" 64
    printf '%s/%s\n' "$directory" "$(basename "$file")"
}

while (($# > 0)); do
    case "$1" in
        --parent-root)
            (($# >= 2)) || fail "--parent-root requires a directory" 64
            parent_root="$2"
            parent_root_explicit=true
            shift 2
            ;;
        --compose-file)
            (($# >= 2)) || fail "--compose-file requires a file" 64
            compose_file="$2"
            shift 2
            ;;
        --dry-run)
            dry_run=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1" 64
            ;;
    esac
done

parent_root="$(resolve_directory "$parent_root")"

if [[ -n "$compose_file" ]]; then
    if [[ "$compose_file" != /* ]]; then
        compose_file="$parent_root/$compose_file"
    fi
    compose_file="$(resolve_file "$compose_file")"
    if [[ "$parent_root_explicit" == false ]]; then
        parent_root="$(cd "$(dirname "$compose_file")" && pwd -P)"
    fi
else
    candidates=()
    for candidate in compose.yml compose.yaml docker-compose.yml docker-compose.yaml; do
        if [[ -f "$parent_root/$candidate" ]]; then
            candidates+=("$parent_root/$candidate")
        fi
    done
    case "${#candidates[@]}" in
        0)
            fail "no parent Compose file found in $parent_root (expected compose.yml, compose.yaml, docker-compose.yml, or docker-compose.yaml)" 64
            ;;
        1)
            compose_file="$(resolve_file "${candidates[0]}")"
            ;;
        *)
            fail "multiple parent Compose files found in $parent_root; pass --compose-file explicitly" 64
            ;;
    esac
fi

[[ -r "$override_file" ]] || fail "Control Center Compose override is missing: $override_file" 64
[[ -x "$verifier" ]] || fail "Control Center loopback verifier is not executable: $verifier" 64

if [[ "$dry_run" == true ]]; then
    printf 'parent_root=%s\n' "$parent_root"
    printf 'compose_file=%s\n' "$compose_file"
    printf 'override_file=%s\n' "$override_file"
    printf 'service=telegram-bot\n'
    printf 'verifier=%s\n' "$verifier"
    exit 0
fi

command -v docker >/dev/null 2>&1 || fail "docker is required" 69
docker compose version >/dev/null 2>&1 || fail "Docker Compose is required" 69

docker compose \
    --project-directory "$parent_root" \
    -f "$compose_file" \
    -f "$override_file" \
    up -d --build --no-deps telegram-bot

"$verifier"
