# AliceTG Bot

Telegram assistant for Home Assistant. The bot is named **Алиса** and speaks as a soft home companion.

## Features

- aiogram 3 Telegram webhook bot.
- aiogram 3 Telegram bot in polling mode by default.
- Optional webhook mode.
- aiohttp server on port `8088` for health and internal endpoints.
- Telegram webhook endpoint: `/webhook` behind Caddy path `/tg/webhook`.
- Internal Home Assistant endpoint: `/internal/coffee/sonya-answer` behind Caddy path `/tg/internal/coffee/sonya-answer`.
- Home Assistant REST API client.
- Whitelist by Telegram `user_id`.
- Telegram webhook `secret_token` check.
- Internal endpoint header `X-Internal-Secret`.
- In-memory reminders for MVP.
- Storage interface prepared for a future SQLite backend.
- Optional HTTP proxy for all outgoing Telegram Bot API requests.
- Styled Telegram buttons for supported clients and Bot API versions.

No Redis, Postgres, Node-RED, Grafana, InfluxDB, or MariaDB are used.

## Telegram Proxy

For Russia, use `TELEGRAM_MODE=polling` and a proxy. Outgoing requests from the bot to Telegram Bot API are usually blocked without a proxy.

Set:

```env
TELEGRAM_PROXY=login:pass@host:port
```

The application converts this value to:

```text
http://login:pass@host:port
```

The proxy is used only for Telegram API requests through aiogram `AiohttpSession`. Home Assistant API requests do not use this proxy. The proxy value is not logged because it contains credentials.

If `TELEGRAM_PROXY` is empty, the bot works without proxy.

## Telegram Mode

Default mode is polling:

```env
TELEGRAM_MODE=polling
TELEGRAM_DROP_PENDING_UPDATES=true
TELEGRAM_POLLING_TIMEOUT=30
TELEGRAM_POLLING_MAX_ERRORS=10
```

Polling uses the same `TELEGRAM_PROXY` for Telegram API requests. The aiohttp server still starts in polling mode, so `/health` and `/internal/*` endpoints remain available.

If polling gets repeated network errors or reconnect failures, the bot logs the exception. When consecutive polling errors reach `TELEGRAM_POLLING_MAX_ERRORS`, the process exits with code `1`. Docker `restart: unless-stopped` then restarts the container.

Webhook mode is optional:

```env
TELEGRAM_MODE=webhook
```

In webhook mode, configure Telegram webhook to:

```text
https://ha.myhomeassistantisverybest.art/tg/webhook
```

## Styled Buttons

The bot sends button styles through the Bot API field `style`:

- `success` for confirmation and safe positive actions;
- `danger` for cancel, delete, reject, turn off;
- `primary` for navigation, refresh, postpone, and choices.

Color rendering depends on the current Telegram client and Bot API support. If a client does not render colors, the same buttons still work as ordinary inline buttons because `callback_data` is unchanged.

## Environment

Copy `.env.example` to `.env` on the server and fill values:

```env
# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_ALLOWED_USER_IDS=
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_MODE=polling
TELEGRAM_DROP_PENDING_UPDATES=true
TELEGRAM_POLLING_TIMEOUT=30
TELEGRAM_POLLING_MAX_ERRORS=10

# HTTP proxy for Telegram API requests only.
TELEGRAM_PROXY=login:pass@host:port

# Home Assistant
HA_URL=http://homeassistant:8123
HA_LONG_LIVED_TOKEN=

# Internal webhook security between Home Assistant and bot
INTERNAL_WEBHOOK_SECRET=
```

`TELEGRAM_ALLOWED_USER_IDS` accepts comma-separated IDs, for example:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

## Create Telegram Bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Choose bot name and username.
4. Copy the token into `TELEGRAM_BOT_TOKEN`.
5. Generate a random webhook secret and put it into `TELEGRAM_WEBHOOK_SECRET`.

Example local generation:

```bash
openssl rand -hex 32
```

## Get Telegram user_id

Use one of these options:

- message `@userinfobot`;
- temporarily log incoming Telegram updates while testing;
- use Telegram API `getUpdates` before webhook is configured.

Put your user ID into:

```env
TELEGRAM_ALLOWED_USER_IDS=
TELEGRAM_ADMIN_CHAT_ID=
```

For MVP, admin chat ID is usually the same as your user ID.

## Create Home Assistant Long-Lived Token

1. Open Home Assistant.
2. Click your user profile.
3. Scroll to `Long-lived access tokens`.
4. Create a token for this bot.
5. Put it into `HA_LONG_LIVED_TOKEN`.

Do not commit the real token.

## Docker Compose

The parent Home Assistant compose project should include:

```yaml
telegram-bot:
  build:
    context: ./TG_Alisa_Assistant_Bot
  container_name: telegram-bot
  restart: unless-stopped
  env_file:
    - .env
  expose:
    - "8088"
  networks:
    - ha_net
```

The bot does not publish a host port. Caddy reaches it inside Docker network.

## Caddy

Caddy should route `/tg/*` to the bot and everything else to Home Assistant:

```caddyfile
ha.myhomeassistantisverybest.art {
	handle_path /tg/* {
		reverse_proxy telegram-bot:8088
	}

	handle {
		reverse_proxy homeassistant:8123
	}
}
```

`handle_path` strips `/tg`, so Telegram webhook URL `/tg/webhook` reaches the bot as `/webhook`.

## Set Telegram Webhook

Webhook is optional. For Russia, polling is usually simpler and more reliable because the bot makes outgoing requests through `TELEGRAM_PROXY`.

If you switch to `TELEGRAM_MODE=webhook`, run from the server after `.env` is filled:

```bash
set -a
. ./.env
set +a

curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://ha.myhomeassistantisverybest.art/tg/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Check webhook:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

If Telegram API is blocked from the server, use the same HTTP proxy manually for these setup checks:

```bash
curl -x "http://$TELEGRAM_PROXY" "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

## Check Internal Endpoint

From inside the compose project on the server:

```bash
set -a
. ./.env
set +a

curl -X POST "http://localhost/tg/internal/coffee/sonya-answer" \
  -H "Content-Type: application/json" \
  -H "X-Internal-Secret: $INTERNAL_WEBHOOK_SECRET" \
  -d '{"answer":"да","intent":"YANDEX.CONFIRM","source":"manual"}'
```

If running from another container in the same Docker network, use:

```text
http://telegram-bot:8088/internal/coffee/sonya-answer
```

## Home Assistant Automation

Use `rest_command`; it is the cleanest Home Assistant-native way to POST JSON to the bot without shell commands.

Add this to `configuration.yaml`:

```yaml
rest_command:
  telegram_bot_sonya_answer:
    url: "http://telegram-bot:8088/internal/coffee/sonya-answer"
    method: POST
    content_type: "application/json"
    headers:
      X-Internal-Secret: !secret telegram_bot_internal_secret
    payload: >-
      {
        "answer": {{ answer | to_json }},
        "intent": {{ intent | to_json }},
        "source": "bedroom"
      }
```

Add this to `secrets.yaml`:

```yaml
telegram_bot_internal_secret: "put-the-same-value-as-INTERNAL_WEBHOOK_SECRET"
```

Add this automation to `automations.yaml`:

```yaml
- id: tg_forward_sonya_coffee_answer
  alias: "Telegram: передать ответ Сони про кофе"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
      event_data:
        session:
          dialog: tg_ask_sonya_wants_coffee
  action:
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
    - action: rest_command.telegram_bot_sonya_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
```

If Home Assistant cannot resolve Docker DNS `telegram-bot`, use the external URL instead:

```yaml
url: "https://ha.myhomeassistantisverybest.art/tg/internal/coffee/sonya-answer"
```

This automation is separate from the existing voice flow. The current `ask_sonya_wants_coffee` automation can continue to auto-enable the coffee machine and reply to the living room.

## Run Locally

Install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Syntax check:

```bash
python -m compileall app
```

Run:

```bash
python -m app.main
```

## Deploy

From the parent Home Assistant compose project on the VPS:

```bash
git pull
docker compose up -d --build
docker compose logs --tail=100 telegram-bot
```

## Rollback If Caddy Breaks

Keep an SSH session open while changing Caddy. If routing fails:

1. Restore the previous `Caddyfile`.
2. Run:

```bash
docker compose restart caddy
docker compose logs --tail=100 caddy
```

If the bot container fails, Home Assistant should still be reachable through the fallback `handle` block after Caddy is restored.

## MVP Limitations

Reminder tasks are stored in memory through `asyncio.create_task`. They are lost when the bot container restarts. The storage interface is intentionally separated so SQLite can replace in-memory storage later.
