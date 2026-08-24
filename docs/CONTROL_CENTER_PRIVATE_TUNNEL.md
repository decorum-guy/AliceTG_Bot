# Artem Control Center private tunnel

The AliceTG Bot internal Control Center API must stay outside Caddy and the
public internet. The Samsung reaches Home Assistant and AliceTG through one
dedicated SSH account with local forwarding only.

## Loopback Docker exposure

Routine Alice updates should use the canonical helper from the Alice checkout.
It always composes the parent Home Assistant project with the tracked loopback
override, rebuilds only `telegram-bot`, and runs the verifier afterward:

```bash
cd ~/homeassistant/TG_Alisa_Assistant_Bot
./scripts/deploy-telegram-bot.sh --parent-root ..
```

For topology explanation or recovery, the equivalent raw Compose shape is:

```bash
docker compose \
  -f compose.yml \
  -f TG_Alisa_Assistant_Bot/deploy/compose.control-center-loopback.yml \
  up -d --build --no-deps telegram-bot
```

The exact parent compose file name can differ. The resulting host listeners
must be:

```text
127.0.0.1:18123 -> homeassistant:8123
127.0.0.1:18088 -> telegram-bot:8088
```

They must not listen on `0.0.0.0`, `[::]` or a public interface. Caddy remains
unchanged and must not proxy `/internal/*`.

Verify without printing response bodies or tokens:

```bash
./TG_Alisa_Assistant_Bot/scripts/verify-control-center-loopback.sh
```

Do not replace the routine helper with a parent-only `docker compose up` for
`telegram-bot`; that omits the loopback override and can remove the private
`127.0.0.1:18088` listener on recreation.

## Dedicated SSH account

The Windows installer creates an ed25519 key and writes its public half to:

```text
%LOCALAPPDATA%\ArtemControlCenter\connectivity-public-key.txt
```

Install that one public key on the VPS through stdin:

```bash
sudo ./TG_Alisa_Assistant_Bot/scripts/install-control-center-tunnel-user.sh \
  < /path/to/connectivity-public-key.txt
```

The installer creates system account `artem-cc-tunnel` and an sshd Match block
with these restrictions:

- public-key authentication only;
- local TCP forwarding only;
- destinations limited to `127.0.0.1:18123` and `127.0.0.1:18088`;
- no shell/session channels (`MaxSessions 0`);
- no PTY, agent forwarding, X11, tunnel devices or gateway binding;
- no password or keyboard-interactive login.

The existing Mac administration account and key are not reused.

The script validates the complete sshd configuration with `sshd -t` before
reloading SSH and restores the prior drop-in if validation fails.

## Secret boundaries

The Samsung needs three independent secrets after the tunnel works:

1. a dedicated Home Assistant long-lived token;
2. `CONTROL_CENTER_API_TOKEN` from AliceTG Bot;
3. `INTERNAL_WEBHOOK_SECRET` from AliceTG Bot for sanitized health details.

Do not reuse `SHORTCUTS_SECRET_TOKEN`. Do not put any of these values in Git,
SSH config, task arguments, command history or logs. Enter them only through the
secure prompts in Control Center's `configure-home-production.ps1`.

## Rotation and removal

To rotate the tunnel key, remove the marked block from:

```text
/var/lib/artem-cc-tunnel/.ssh/authorized_keys
```

Then delete the local Windows key pair and rerun both installers. The sshd
Match block can remain because it contains no credential.

To remove the boundary completely:

1. stop the Windows connectivity task;
2. remove the dedicated system user's authorized key;
3. remove `/etc/ssh/sshd_config.d/90-artem-control-center-tunnel.conf`;
4. run `sshd -t` and reload SSH;
5. remove the loopback compose override and recreate the two containers.
