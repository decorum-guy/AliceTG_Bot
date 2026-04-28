# AliceTG Bot

Telegram assistant for Home Assistant. The bot is named **Алиса** and speaks as a soft home companion.

## Features

- aiogram 3 Telegram webhook bot.
- aiogram 3 Telegram bot in polling mode by default.
- Optional webhook mode.
- aiohttp server on port `8088` for health and internal endpoints.
- Telegram webhook endpoint: `/webhook` behind Caddy path `/tg/webhook`.
- Internal Home Assistant endpoints under `/internal/coffee/*` behind Caddy path `/tg/internal/coffee/*`.
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
TELEGRAM_ENABLE_TEST_1_MIN_REMINDER=false
```

Polling uses the same `TELEGRAM_PROXY` for Telegram API requests. The aiohttp server still starts in polling mode, so `/health` and `/internal/*` endpoints remain available.

If polling gets repeated network errors or reconnect failures, the bot logs the exception. When consecutive polling errors reach `TELEGRAM_POLLING_MAX_ERRORS`, the process exits with code `1`. Docker `restart: unless-stopped` then restarts the container.

For quick manual testing, set `TELEGRAM_ENABLE_TEST_1_MIN_REMINDER=true`. This adds a `+1 минута` button to the "Попозже" reminder menu. Keep it `false` in normal use.

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
TELEGRAM_SONYA_USER_IDS=
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_MODE=polling
TELEGRAM_DROP_PENDING_UPDATES=true
TELEGRAM_POLLING_TIMEOUT=30
TELEGRAM_POLLING_MAX_ERRORS=10
TELEGRAM_ENABLE_TEST_1_MIN_REMINDER=false

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

`TELEGRAM_SONYA_USER_IDS` is optional. If it is set, those users see only the order menu:

```env
TELEGRAM_SONYA_USER_IDS=222222222
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

## Check Internal Endpoints

From inside the compose project on the server:

```bash
set -a
. ./.env
set +a

curl -X POST "http://localhost/tg/internal/coffee/sonya-wants-answer" \
  -H "Content-Type: application/json" \
  -H "X-Internal-Secret: $INTERNAL_WEBHOOK_SECRET" \
  -d '{"answer":"да","intent":"YANDEX.CONFIRM","dialog":"tg_ask_sonya_wants_coffee"}'
```

If running from another container in the same Docker network, use:

```text
http://telegram-bot:8088/internal/coffee/sonya-wants-answer
http://telegram-bot:8088/internal/coffee/sonya-type-answer
http://telegram-bot:8088/internal/coffee/sonya-direct-type-answer
```

## Current Home Assistant Automation

Use this section as the source of truth. Remove the old Telegram automation blocks before adding these.

Existing IDs to replace or remove:

- Replace `ask_sonya_about_coffee` fully with the complete `automations.yaml` content below.
- Replace `tg_sonya_wants_coffee_answer` with the complete `automations.yaml` content below.
- Delete old `tg_sonya_coffee_type_answer`.
- Delete old `tg_sonya_direct_coffee_type_answer`.
- Replace `tg_sonya_direct_coffee_request` with the block below.

The split flow now uses these IDs:

- `tg_sonya_temperature_answer`
- `tg_sonya_syrup_answer`
- `tg_sonya_direct_temperature_answer`
- `tg_sonya_direct_syrup_answer`

Add helpers to `configuration.yaml`:

```yaml
input_boolean:
  tg_awaiting_sonya_coffee_temperature:
    name: Telegram awaiting Sonya coffee temperature
  tg_awaiting_sonya_coffee_syrup:
    name: Telegram awaiting Sonya coffee syrup
  sonya_direct_awaiting_coffee_temperature:
    name: Sonya direct awaiting coffee temperature
  sonya_direct_awaiting_coffee_syrup:
    name: Sonya direct awaiting coffee syrup
```

Add or replace `rest_command` in `configuration.yaml`:

```yaml
rest_command:
  tg_sonya_wants_coffee_answer:
    url: "http://telegram-bot:8088/internal/coffee/sonya-wants-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_temperature_answer:
    url: "http://telegram-bot:8088/internal/coffee/sonya-temperature-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_syrup_answer:
    url: "http://telegram-bot:8088/internal/coffee/sonya-syrup-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_auto_enabled:
    url: "http://telegram-bot:8088/internal/coffee/sonya-auto-enabled"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}
```

Add to `secrets.yaml`:

```yaml
internal_webhook_secret: "put-the-same-value-as-INTERNAL_WEBHOOK_SECRET"
```

Use this as the full ready-to-copy content of `config/automations.yaml`. Going forward, when this workflow changes, update this complete file block rather than separate fragments.

```yaml
- id: ask_sonya_about_coffee
  alias: "Спросить Соню про кофе"
  mode: single
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set text = trigger.event.data.text | default('') | lower %}
        {{ 'сон' in text and 'кофе' in text }}
  action:
    - action: media_player.play_media
      target:
        entity_id: media_player.stantsiia_mini_spalnia
      data:
        media_content_id: "Соня, тебя спрашивают, хочешь ли ты кофе?"
        media_content_type: "dialog:домашний помощник:ask_sonya_wants_coffee"
    - wait_for_trigger:
        - platform: event
          event_type: yandex_intent
          event_data:
            session:
              dialog: ask_sonya_wants_coffee
      timeout: "00:00:30"
      continue_on_timeout: true
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ wait.trigger is none }}"
          sequence:
            - action: media_player.play_media
              target:
                entity_id: media_player.stantsiia_mini_zal
              data:
                media_content_id: "Соня не ответила."
                media_content_type: "text"
      default:
        - variables:
            wants_answer: "{{ wait.trigger.event.data.text | default('') | lower }}"
            wants_intent: "{{ wait.trigger.event.data.intent | default('') }}"
        - choose:
            - conditions:
                - condition: template
                  value_template: >-
                    {% set positive_words = ['да', 'хочу', 'ага', 'можно', 'буду', 'конечно'] %}
                    {{ wants_intent == 'YANDEX.CONFIRM' or positive_words | select('in', wants_answer) | list | count > 0 }}
              sequence:
                - action: switch.turn_on
                  target:
                    entity_id: switch.kofemashina
                - action: media_player.play_media
                  target:
                    entity_id: media_player.stantsiia_mini_spalnia
                  data:
                    media_content_id: "Какой кофе ты хочешь: горячий или холодный?"
                    media_content_type: "dialog:домашний помощник:ask_sonya_coffee_temperature"
                - wait_for_trigger:
                    - platform: event
                      event_type: yandex_intent
                  timeout: "00:00:30"
                  continue_on_timeout: true
                - choose:
                    - conditions:
                        - condition: template
                          value_template: >-
                            {% if wait.trigger is none %}
                              true
                            {% else %}
                              {% set text = wait.trigger.event.data.text | default('') | lower %}
                              {% set session = wait.trigger.event.data.session | default({}) %}
                              {% set dialog = session.dialog | default('') %}
                              {% set is_service_phrase = 'скажи навыку' in text or 'домашний помощник' in text %}
                              {{ is_service_phrase or not (dialog == 'ask_sonya_coffee_temperature' or 'холод' in text or 'горяч' in text) }}
                            {% endif %}
                      sequence:
                        - action: media_player.play_media
                          target:
                            entity_id: media_player.stantsiia_mini_zal
                          data:
                            media_content_id: "Соня хочет кофе, но не сказала какой."
                            media_content_type: "text"
                  default:
                    - variables:
                        temperature_answer: "{{ wait.trigger.event.data.text | default('') | lower }}"
                        coffee_temperature: >-
                          {% if 'холод' in temperature_answer %}холодный кофе{% elif 'горяч' in temperature_answer %}горячий кофе{% else %}{{ temperature_answer }}{% endif %}
                    - action: media_player.play_media
                      target:
                        entity_id: media_player.stantsiia_mini_spalnia
                      data:
                        media_content_id: "С сиропом или без?"
                        media_content_type: "dialog:домашний помощник:ask_sonya_coffee_syrup"
                    - wait_for_trigger:
                        - platform: event
                          event_type: yandex_intent
                      timeout: "00:00:30"
                      continue_on_timeout: true
                    - choose:
                        - conditions:
                            - condition: template
                              value_template: >-
                                {% if wait.trigger is none %}
                                  true
                                {% else %}
                                  {% set text = wait.trigger.event.data.text | default('') | lower %}
                                  {% set session = wait.trigger.event.data.session | default({}) %}
                                  {% set dialog = session.dialog | default('') %}
                                  {% set is_service_phrase = 'скажи навыку' in text or 'домашний помощник' in text %}
                                  {{ is_service_phrase or not (dialog == 'ask_sonya_coffee_syrup' or 'сироп' in text or 'без' in text or text in ['да', 'нет', 'хочу', 'не хочу']) }}
                                {% endif %}
                          sequence:
                            - variables:
                                syrup_answer: ""
                                coffee_syrup: "не указано"
                      default:
                        - variables:
                            syrup_answer: "{{ wait.trigger.event.data.text | default('') | lower }}"
                            coffee_syrup: >-
                              {% if 'не хочу' in syrup_answer or 'нет' in syrup_answer or 'без' in syrup_answer %}без сиропа{% elif syrup_answer in ['да', 'хочу'] or 'сироп' in syrup_answer %}с сиропом{% else %}{{ syrup_answer }}{% endif %}
                    - action: media_player.play_media
                      target:
                        entity_id: media_player.stantsiia_mini_zal
                      data:
                        media_content_id: "Соня сказала, что хочет {{ coffee_temperature }} {{ coffee_syrup }}."
                        media_content_type: "text"
                    - action: rest_command.tg_sonya_auto_enabled
                      data:
                        answer: "{{ coffee_temperature }} {{ coffee_syrup }}"
                        intent: ""
                        dialog: "ask_sonya_coffee_syrup"
            - conditions:
                - condition: template
                  value_template: >-
                    {% set negative_phrases = ['нет спасибо', 'не хочу', 'не надо', 'нет'] %}
                    {{ wants_intent == 'YANDEX.REJECT' or negative_phrases | select('in', wants_answer) | list | count > 0 }}
              sequence:
                - action: media_player.play_media
                  target:
                    entity_id: media_player.stantsiia_mini_zal
                  data:
                    media_content_id: "Соня сказала, что кофе не хочет."
                    media_content_type: "text"
          default:
            - action: media_player.play_media
              target:
                entity_id: media_player.stantsiia_mini_zal
              data:
                media_content_id: "Соня ответила: {{ wants_answer }}. Я не понял, включать кофемашину или нет."
                media_content_type: "text"

- id: tg_sonya_wants_coffee_answer
  alias: "Telegram bot - ответ Сони хочет ли кофе"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {{ dialog == 'tg_ask_sonya_wants_coffee' }}
  action:
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        dialog: "tg_ask_sonya_wants_coffee"
    - action: rest_command.tg_sonya_wants_coffee_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_temperature_answer
  alias: "Telegram bot - ответ Сони температура кофе"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {% set text = trigger.event.data.text | default('') | lower %}
        {% set is_service_phrase = 'скажи навыку' in text or 'домашний помощник' in text %}
        {{ not is_service_phrase and (dialog == 'tg_ask_sonya_coffee_temperature'
           or (is_state('input_boolean.tg_awaiting_sonya_coffee_temperature', 'on')
               and ('холод' in text or 'горяч' in text))) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.tg_awaiting_sonya_coffee_temperature
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        dialog: "tg_ask_sonya_coffee_temperature"
    - action: rest_command.tg_sonya_temperature_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_syrup_answer
  alias: "Telegram bot - ответ Сони сироп"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {% set text = trigger.event.data.text | default('') | lower %}
        {% set is_service_phrase = 'скажи навыку' in text or 'домашний помощник' in text %}
        {% set is_valid_syrup = 'сироп' in text or 'без' in text or text in ['да', 'нет', 'хочу', 'не хочу'] %}
        {{ not is_service_phrase and (dialog == 'tg_ask_sonya_coffee_syrup'
           or (is_state('input_boolean.tg_awaiting_sonya_coffee_syrup', 'on') and is_valid_syrup)) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.tg_awaiting_sonya_coffee_syrup
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        dialog: "tg_ask_sonya_coffee_syrup"
    - action: rest_command.tg_sonya_syrup_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_direct_coffee_request
  alias: "Telegram bot - Соня сама просит кофе"
  mode: single
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set text = trigger.event.data.text | default('') | lower %}
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {% set is_service_phrase = 'скажи навыку' in text or 'домашний помощник' in text %}
        {{ not is_service_phrase
           and 'кофе' in text
           and 'спроси сон' not in text
           and 'узнай' not in text
           and dialog not in [
             'tg_ask_sonya_wants_coffee',
             'tg_ask_sonya_coffee_temperature',
             'tg_ask_sonya_coffee_syrup',
             'ask_sonya_wants_coffee',
             'ask_sonya_coffee_temperature',
             'ask_sonya_coffee_syrup',
             'sonya_direct_coffee_temperature',
             'sonya_direct_coffee_syrup'
           ] }}
  action:
    - action: input_boolean.turn_on
      target:
        entity_id: input_boolean.sonya_direct_awaiting_coffee_temperature
    - action: media_player.play_media
      target:
        entity_id: media_player.stantsiia_mini_spalnia
      data:
        media_content_id: "Какой кофе ты хочешь: горячий или холодный?"
        media_content_type: "dialog:домашний помощник:sonya_direct_coffee_temperature"

- id: tg_sonya_direct_temperature_answer
  alias: "Telegram bot - прямой ответ Сони температура кофе"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {% set text = trigger.event.data.text | default('') | lower %}
        {% set is_service_phrase = 'скажи навыку' in text or 'домашний помощник' in text %}
        {{ not is_service_phrase and (dialog == 'sonya_direct_coffee_temperature'
           or (is_state('input_boolean.sonya_direct_awaiting_coffee_temperature', 'on')
               and ('холод' in text or 'горяч' in text))) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.sonya_direct_awaiting_coffee_temperature
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        dialog: "sonya_direct_coffee_temperature"
    - action: rest_command.tg_sonya_temperature_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_direct_syrup_answer
  alias: "Telegram bot - прямой ответ Сони сироп"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {% set text = trigger.event.data.text | default('') | lower %}
        {% set is_service_phrase = 'скажи навыку' in text or 'домашний помощник' in text %}
        {% set is_valid_syrup = 'сироп' in text or 'без' in text or text in ['да', 'нет', 'хочу', 'не хочу'] %}
        {{ not is_service_phrase and (dialog == 'sonya_direct_coffee_syrup'
           or (is_state('input_boolean.sonya_direct_awaiting_coffee_syrup', 'on') and is_valid_syrup)) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.sonya_direct_awaiting_coffee_syrup
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        dialog: "sonya_direct_coffee_syrup"
    - action: rest_command.tg_sonya_syrup_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"
```
Check and restart:

```bash
docker compose exec homeassistant python -m homeassistant --script check_config --config /config
docker compose restart homeassistant
docker compose up -d --build telegram-bot
```
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
cd ~/homeassistant/TG_Alisa_Assistant_Bot
git pull

cd ~/homeassistant
docker compose up -d --build telegram-bot
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
