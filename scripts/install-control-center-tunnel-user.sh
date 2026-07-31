#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly TUNNEL_USER="${CONTROL_CENTER_TUNNEL_USER:-artem-cc-tunnel}"
readonly HOME_DIR="${CONTROL_CENTER_TUNNEL_HOME:-/var/lib/$TUNNEL_USER}"
readonly HA_PORT="${CONTROL_CENTER_HA_LOOPBACK_PORT:-18123}"
readonly BOT_PORT="${CONTROL_CENTER_BOT_LOOPBACK_PORT:-18088}"
readonly SSHD_DROPIN="/etc/ssh/sshd_config.d/90-artem-control-center-tunnel.conf"
readonly BEGIN_MARKER="# BEGIN ARTEM CONTROL CENTER TUNNEL KEY"
readonly END_MARKER="# END ARTEM CONTROL CENTER TUNNEL KEY"

fail() {
    printf 'error: %s\n' "$1" >&2
    exit "${2:-1}"
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run as root" 77
[[ "$TUNNEL_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || fail "invalid tunnel user" 64
[[ "$HA_PORT" =~ ^[0-9]+$ && "$BOT_PORT" =~ ^[0-9]+$ ]] || fail "invalid loopback port" 64
(( HA_PORT >= 1 && HA_PORT <= 65535 && BOT_PORT >= 1 && BOT_PORT <= 65535 )) || fail "loopback port out of range" 64
(( HA_PORT != BOT_PORT )) || fail "loopback ports must be different" 64

IFS= read -r public_key || fail "read one OpenSSH public key from stdin" 64
[[ -n "$public_key" ]] || fail "public key is empty" 64
[[ "$public_key" != *$'\n'* && "$public_key" != *$'\r'* ]] || fail "public key must be one line" 64
[[ "$public_key" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || fail "only an ssh-ed25519 public key is accepted" 64

if ! id "$TUNNEL_USER" >/dev/null 2>&1; then
    useradd \
        --system \
        --create-home \
        --home-dir "$HOME_DIR" \
        --shell /usr/sbin/nologin \
        "$TUNNEL_USER"
fi

readonly USER_GROUP="$(id -gn "$TUNNEL_USER")"
readonly SSH_DIR="$HOME_DIR/.ssh"
readonly AUTHORIZED_KEYS="$SSH_DIR/authorized_keys"
install -d -m 700 -o "$TUNNEL_USER" -g "$USER_GROUP" "$SSH_DIR"
touch "$AUTHORIZED_KEYS"
chown "$TUNNEL_USER:$USER_GROUP" "$AUTHORIZED_KEYS"
chmod 600 "$AUTHORIZED_KEYS"

key_options="restrict,port-forwarding,permitopen=\"127.0.0.1:$HA_PORT\",permitopen=\"127.0.0.1:$BOT_PORT\""
managed_line="$key_options $public_key"
temporary_keys="$(mktemp "$SSH_DIR/.authorized_keys.XXXXXX")"
cleanup() {
    rm -f "$temporary_keys" "${temporary_dropin:-}"
}
trap cleanup EXIT

awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { managed=1; next }
    $0 == end { managed=0; next }
    !managed { print }
' "$AUTHORIZED_KEYS" >"$temporary_keys"
{
    printf '%s\n' "$BEGIN_MARKER"
    printf '%s\n' "$managed_line"
    printf '%s\n' "$END_MARKER"
} >>"$temporary_keys"
install -m 600 -o "$TUNNEL_USER" -g "$USER_GROUP" "$temporary_keys" "$AUTHORIZED_KEYS"

install -d -m 755 /etc/ssh/sshd_config.d
temporary_dropin="$(mktemp /etc/ssh/sshd_config.d/.90-artem-control-center-tunnel.XXXXXX)"
cat >"$temporary_dropin" <<EOF
# Managed by AliceTG_Bot/scripts/install-control-center-tunnel-user.sh
Match User $TUNNEL_USER
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:$HA_PORT 127.0.0.1:$BOT_PORT
    GatewayPorts no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    MaxSessions 0
EOF
chmod 600 "$temporary_dropin"

backup=""
if [[ -e "$SSHD_DROPIN" ]]; then
    backup="$(mktemp /etc/ssh/sshd_config.d/.90-artem-control-center-tunnel.backup.XXXXXX)"
    cp -a "$SSHD_DROPIN" "$backup"
fi
install -m 600 "$temporary_dropin" "$SSHD_DROPIN"
if ! sshd -t; then
    if [[ -n "$backup" ]]; then
        install -m 600 "$backup" "$SSHD_DROPIN"
    else
        rm -f "$SSHD_DROPIN"
    fi
    rm -f "$backup"
    fail "sshd rejected the restricted tunnel configuration" 78
fi
rm -f "$backup"

if command -v systemctl >/dev/null 2>&1; then
    if systemctl reload ssh >/dev/null 2>&1; then
        :
    elif systemctl reload sshd >/dev/null 2>&1; then
        :
    else
        fail "unable to reload ssh service" 69
    fi
else
    service ssh reload >/dev/null 2>&1 || service sshd reload >/dev/null 2>&1 || fail "unable to reload ssh service" 69
fi

printf 'Installed forwarding-only SSH account: %s\n' "$TUNNEL_USER"
printf 'Allowed destination: 127.0.0.1:%s (Home Assistant)\n' "$HA_PORT"
printf 'Allowed destination: 127.0.0.1:%s (AliceTG Bot)\n' "$BOT_PORT"
printf 'Shell, PTY, agent, X11, remote forwarding and arbitrary destinations are disabled.\n'
