# AliceTG Bot

Telegram assistant for Home Assistant. The bot is named **Алиса** and speaks as a soft home companion.

## What This Bot Does

- Telegram bot for Home Assistant smart home control.
- Coffee workflow: Artem can ask Sonya, Sonya can order by voice, hall/zal can ask Sonya, and Sonya can order from her Telegram menu.
- Tea/kettle workflow: Artem can ask Sonya, Sonya can order by voice, hall/zal can ask Sonya, and Sonya can order from her Telegram menu.
- Separate Telegram menus for Artem and Sonya.
- Yandex Station dialog integration through Home Assistant `media_player.play_media`.
- aiohttp server on port `8088` for health checks and internal Home Assistant endpoints.
- Home Assistant REST API client, reminders, Telegram proxy support, and styled Telegram buttons.

No Redis, Postgres, Node-RED, Grafana, InfluxDB, or MariaDB are used.

## Runtime Behavior

### Artem Menu

Main menu:

- `Умные устройства`
- `Спросить Соню`

`Умные устройства` contains:

- `Кофемашина`
- `Чайник`
- `Назад`

### Sonya Menu

Sonya sees only her own order menu:

- `☕️ Кофе`
- `🍵 Чай`

Sonya does not see `Спросить Соню`, `Умные устройства`, direct coffee machine control, or direct kettle control.

### Coffee Workflow

- Telegram-initiated coffee flow may edit the active Telegram message after each step.
- Direct voice coffee flow does not send an intermediate Telegram message after temperature. It creates the Telegram confirmation only after syrup is received.
- Hall/zal voice flow is separate from direct voice flow. It asks whether Sonya wants coffee first, auto-enables the coffee machine after a positive answer, and sends Telegram only an info notification with `Удалить уведомление`.
- Sonya Telegram order uses `source=sonya_telegram_order`, so the bedroom does not say `Твой кофе скоро будет готов` after Artem presses `Да`.
- Voice-based coffee flows say a short bedroom acknowledgement after syrup, for example `Хорошо, горячий кофе без сиропа, поняла.` Confirmation speech is delayed briefly so it does not overlap.
- Before Artem confirms, the coffee machine does not turn on, except in the hall/zal voice flow where it turns on automatically after Sonya says yes.
- If an internal coffee event fails while being processed, the bot logs `Coffee workflow failed, resetting coffee flags` and resets all coffee wait flags through Home Assistant.

### Coffee Alerts

- If `switch.kofemashina` stays on continuously for 15 minutes, Telegram receives a warmed-up alert with a `Выключить` button.
- If `switch.kofemashina` turns off before 15 minutes, the warmed-up alert is not sent.
- If `switch.kofemashina` stays on continuously for 1 hour, Telegram receives a warning alert with a `Выключить` button.
- Coffee machine status shows continuous running time while the switch is on; it shows a dash when the switch is off.

### Tea And Kettle Workflow

- Telegram ask-Sonya tea flow asks whether Sonya wants tea, then asks about keep-warm.
- Direct voice tea flow asks keep-warm first and does not start the kettle before Artem confirms.
- Hall/zal tea flow asks whether Sonya wants tea first, starts the kettle automatically after a positive answer, and sends Telegram only an info notification.
- Sonya Telegram tea order does not speak in the bedroom.
- Boil with `water_heater.set_temperature` and `temperature: 100`.
- Stop with `water_heater.set_operation_mode` and `operation_mode: "off"`.
- Do not use `water_heater.turn_on` or `water_heater.turn_off`.
- Keep-warm uses numeric temperatures only: `40`, `50`, `60`, `70`, `80`, `90`.
- To enable keep-warm, first call `water_heater.set_temperature`, then `switch.turn_on switch.chainik_podderzhanie_tepla`.
- Kettle light is `switch.chainik_podsvetka`.
- Kettle mute mode is `switch.chainik_bez_zvuka`.
- `switch.chainik_blokirovka_upravleniia` is intentionally not exposed in Telegram.

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

## Environment Variables

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
YANDEX_DIALOG_SKILL_NAME=домашний помощник

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

`YANDEX_DIALOG_SKILL_NAME` is the Yandex Dialog skill name used in station `media_content_type`
values like `dialog:домашний помощник:tg_ask_sonya_wants_coffee`. The default is
`домашний помощник`, so existing setups keep working.

Home Assistant YAML does not read the bot `.env`. If you rename the Yandex skill, update
`YANDEX_DIALOG_SKILL_NAME` in `.env` and replace all `домашний помощник` occurrences in the
Home Assistant YAML examples below, including `media_content_type` and service phrase checks.

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

## Home Assistant configuration.yaml

Use this section as the source of truth for Home Assistant `configuration.yaml`. Keep secrets in `secrets.yaml`; do not paste secret values into git.

The active automation IDs are:

- `ask_sonya_about_coffee`
- `tg_sonya_wants_coffee_answer`
- `tg_sonya_temperature_answer`
- `tg_sonya_syrup_answer`
- `tg_sonya_direct_coffee_request`
- `tg_sonya_direct_temperature_answer`
- `tg_sonya_direct_syrup_answer`
- `coffee_warmed_up_alert`
- `coffee_long_running_alert`
- `ask_sonya_about_tea`
- `tg_sonya_direct_tea_request`
- `tg_sonya_wants_tea_answer`
- `tg_sonya_tea_keep_warm_answer`
- `tg_sonya_tea_keep_warm_temperature_answer`

Old pre-split coffee/tea automations should be removed before pasting this file so there are no duplicate handlers.

Add to `secrets.yaml`:

```yaml
internal_webhook_secret: "put-the-same-value-as-INTERNAL_WEBHOOK_SECRET"
```

Add or replace in `configuration.yaml`:

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
  hall_awaiting_sonya_coffee_temperature:
    name: Hall awaiting Sonya coffee temperature
  hall_awaiting_sonya_coffee_syrup:
    name: Hall awaiting Sonya coffee syrup
  tg_awaiting_sonya_tea_wants:
    name: Telegram awaiting Sonya tea wants
  tg_awaiting_sonya_tea_keep_warm:
    name: Telegram awaiting Sonya tea keep warm
  tg_awaiting_sonya_tea_keep_warm_temperature:
    name: Telegram awaiting Sonya tea keep warm temperature
  sonya_direct_awaiting_tea_keep_warm:
    name: Sonya direct awaiting tea keep warm
  sonya_direct_awaiting_tea_keep_warm_temperature:
    name: Sonya direct awaiting tea keep warm temperature
  hall_awaiting_sonya_tea_wants:
    name: Hall awaiting Sonya tea wants
  hall_awaiting_sonya_tea_keep_warm:
    name: Hall awaiting Sonya tea keep warm
  hall_awaiting_sonya_tea_keep_warm_temperature:
    name: Hall awaiting Sonya tea keep warm temperature

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

  tg_sonya_hall_refused:
    url: "http://telegram-bot:8088/internal/coffee/sonya-hall-refused"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: "{}"

  tg_coffee_warmed_up_alert:
    url: "http://telegram-bot:8088/internal/coffee/warmed-up-alert"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: "{}"

  tg_coffee_long_running_alert:
    url: "http://telegram-bot:8088/internal/coffee/long-running-alert"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: "{}"

  tg_sonya_wants_tea_answer:
    url: "http://telegram-bot:8088/internal/tea/sonya-wants-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_tea_keep_warm_answer:
    url: "http://telegram-bot:8088/internal/tea/sonya-keep-warm-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_tea_keep_warm_temperature_answer:
    url: "http://telegram-bot:8088/internal/tea/sonya-keep-warm-temperature-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_direct_tea_request:
    url: "http://telegram-bot:8088/internal/tea/sonya-direct-request"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: "{}"

  tg_sonya_hall_tea_request:
    url: "http://telegram-bot:8088/internal/tea/hall-request"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: "{}"

  tg_sonya_tea_auto_enabled:
    url: "http://telegram-bot:8088/internal/tea/sonya-auto-enabled"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_tea_hall_refused:
    url: "http://telegram-bot:8088/internal/tea/sonya-hall-refused"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: "{}"
```

## Home Assistant automations.yaml

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
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_coffee_temperature
          - input_boolean.tg_awaiting_sonya_coffee_syrup
          - input_boolean.sonya_direct_awaiting_coffee_temperature
          - input_boolean.sonya_direct_awaiting_coffee_syrup
          - input_boolean.hall_awaiting_sonya_coffee_temperature
          - input_boolean.hall_awaiting_sonya_coffee_syrup
    - action: media_player.play_media
      target:
        entity_id: media_player.stantsiia_mini_spalnia
      data:
        media_content_id: "Соня, тебя спрашивают: будешь кофе?"
        media_content_type: "dialog:домашний помощник:hall_ask_sonya_wants_coffee"
    - wait_for_trigger:
        - platform: event
          event_type: yandex_intent
          event_data:
            session:
              dialog: hall_ask_sonya_wants_coffee
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
                - action: input_boolean.turn_on
                  target:
                    entity_id: input_boolean.hall_awaiting_sonya_coffee_temperature
                - action: media_player.play_media
                  target:
                    entity_id: media_player.stantsiia_mini_spalnia
                  data:
                    media_content_id: "Какой кофе ты хочешь: горячий или холодный?"
                    media_content_type: "dialog:домашний помощник:hall_sonya_coffee_temperature"
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
                              {% set yandex_dialog_skill_name = 'домашний помощник' %}
                              {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
                              {{ is_service_phrase or not (dialog == 'hall_sonya_coffee_temperature' or (is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on') and ('холод' in text or 'горяч' in text))) }}
                            {% endif %}
                      sequence:
                        - action: input_boolean.turn_off
                          target:
                            entity_id:
                              - input_boolean.tg_awaiting_sonya_coffee_temperature
                              - input_boolean.tg_awaiting_sonya_coffee_syrup
                              - input_boolean.sonya_direct_awaiting_coffee_temperature
                              - input_boolean.sonya_direct_awaiting_coffee_syrup
                              - input_boolean.hall_awaiting_sonya_coffee_temperature
                              - input_boolean.hall_awaiting_sonya_coffee_syrup
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
                    - action: input_boolean.turn_off
                      target:
                        entity_id: input_boolean.hall_awaiting_sonya_coffee_temperature
                    - action: input_boolean.turn_on
                      target:
                        entity_id: input_boolean.hall_awaiting_sonya_coffee_syrup
                    - action: media_player.play_media
                      target:
                        entity_id: media_player.stantsiia_mini_spalnia
                      data:
                        media_content_id: "С сиропом или без?"
                        media_content_type: "dialog:домашний помощник:hall_sonya_coffee_syrup"
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
                                  {% set yandex_dialog_skill_name = 'домашний помощник' %}
                                  {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
                                  {{ is_service_phrase or not (dialog == 'hall_sonya_coffee_syrup' or (is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on') and ('сироп' in text or 'без' in text or text in ['да', 'нет', 'хочу', 'не хочу']))) }}
                                {% endif %}
                          sequence:
                            - action: input_boolean.turn_off
                              target:
                                entity_id:
                                  - input_boolean.tg_awaiting_sonya_coffee_temperature
                                  - input_boolean.tg_awaiting_sonya_coffee_syrup
                                  - input_boolean.sonya_direct_awaiting_coffee_temperature
                                  - input_boolean.sonya_direct_awaiting_coffee_syrup
                                  - input_boolean.hall_awaiting_sonya_coffee_temperature
                                  - input_boolean.hall_awaiting_sonya_coffee_syrup
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
                        entity_id: media_player.stantsiia_mini_spalnia
                      data:
                        media_content_id: "Хорошо, {{ coffee_temperature }} {{ coffee_syrup }}, поняла."
                        media_content_type: "text"
                    - action: input_boolean.turn_off
                      target:
                        entity_id:
                          - input_boolean.tg_awaiting_sonya_coffee_temperature
                          - input_boolean.tg_awaiting_sonya_coffee_syrup
                          - input_boolean.sonya_direct_awaiting_coffee_temperature
                          - input_boolean.sonya_direct_awaiting_coffee_syrup
                          - input_boolean.hall_awaiting_sonya_coffee_temperature
                          - input_boolean.hall_awaiting_sonya_coffee_syrup
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
                        dialog: "hall_sonya_coffee_syrup"
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
                    media_content_id: "Соня отказалась от кофе."
                    media_content_type: "text"
                - action: rest_command.tg_sonya_hall_refused
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
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_coffee_temperature
          - input_boolean.tg_awaiting_sonya_coffee_syrup
          - input_boolean.sonya_direct_awaiting_coffee_temperature
          - input_boolean.sonya_direct_awaiting_coffee_syrup
          - input_boolean.hall_awaiting_sonya_coffee_temperature
          - input_boolean.hall_awaiting_sonya_coffee_syrup
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {% set direct_active = is_state('input_boolean.sonya_direct_awaiting_coffee_temperature', 'on') or is_state('input_boolean.sonya_direct_awaiting_coffee_syrup', 'on') %}
        {% set hall_active = is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on') or is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on') %}
        {{ not is_service_phrase
           and not direct_active
           and not hall_active
           and (dialog == 'tg_ask_sonya_coffee_temperature'
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {% set direct_active = is_state('input_boolean.sonya_direct_awaiting_coffee_temperature', 'on') or is_state('input_boolean.sonya_direct_awaiting_coffee_syrup', 'on') %}
        {% set hall_active = is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on') or is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on') %}
        {% set is_valid_syrup = 'сироп' in text or 'без' in text or text in ['да', 'нет', 'хочу', 'не хочу'] %}
        {{ not is_service_phrase
           and not direct_active
           and not hall_active
           and (dialog == 'tg_ask_sonya_coffee_syrup'
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {% set any_active = is_state('input_boolean.tg_awaiting_sonya_coffee_temperature', 'on')
           or is_state('input_boolean.tg_awaiting_sonya_coffee_syrup', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_coffee_temperature', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_coffee_syrup', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on')
           or is_state('input_boolean.tg_awaiting_sonya_tea_wants', 'on')
           or is_state('input_boolean.tg_awaiting_sonya_tea_keep_warm', 'on')
           or is_state('input_boolean.tg_awaiting_sonya_tea_keep_warm_temperature', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm_temperature', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_wants', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm_temperature', 'on') %}
        {% set intermediate = text in ['горячий', 'холодный', 'горячий кофе', 'холодный кофе', 'с сиропом', 'без сиропа', 'без', 'да', 'нет'] %}
        {{ not is_service_phrase
           and not any_active
           and not intermediate
           and 'кофе' in text
           and 'сон' not in text
           and 'спроси сон' not in text
           and 'узнай' not in text
           and dialog not in [
             'tg_ask_sonya_wants_coffee',
             'tg_ask_sonya_coffee_temperature',
             'tg_ask_sonya_coffee_syrup',
             'ask_sonya_wants_coffee',
             'ask_sonya_coffee_temperature',
             'ask_sonya_coffee_syrup',
             'hall_ask_sonya_wants_coffee',
             'hall_sonya_coffee_temperature',
             'hall_sonya_coffee_syrup',
             'sonya_direct_coffee_temperature',
             'sonya_direct_coffee_syrup'
           ] }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_coffee_temperature
          - input_boolean.tg_awaiting_sonya_coffee_syrup
          - input_boolean.sonya_direct_awaiting_coffee_temperature
          - input_boolean.sonya_direct_awaiting_coffee_syrup
          - input_boolean.hall_awaiting_sonya_coffee_temperature
          - input_boolean.hall_awaiting_sonya_coffee_syrup
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {% set tg_active = is_state('input_boolean.tg_awaiting_sonya_coffee_temperature', 'on') or is_state('input_boolean.tg_awaiting_sonya_coffee_syrup', 'on') %}
        {% set hall_active = is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on') or is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on') %}
        {{ not is_service_phrase
           and not tg_active
           and not hall_active
           and (dialog == 'sonya_direct_coffee_temperature'
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {% set tg_active = is_state('input_boolean.tg_awaiting_sonya_coffee_temperature', 'on') or is_state('input_boolean.tg_awaiting_sonya_coffee_syrup', 'on') %}
        {% set hall_active = is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on') or is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on') %}
        {% set is_valid_syrup = 'сироп' in text or 'без' in text or text in ['да', 'нет', 'хочу', 'не хочу'] %}
        {{ not is_service_phrase
           and not tg_active
           and not hall_active
           and (dialog == 'sonya_direct_coffee_syrup'
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
- id: coffee_warmed_up_alert
  alias: "Telegram bot - кофемашина разогрета"
  mode: single
  trigger:
    - platform: state
      entity_id: switch.kofemashina
      to: "on"
      for: "00:15:00"
  condition:
    - condition: state
      entity_id: switch.kofemashina
      state: "on"
  action:
    - action: rest_command.tg_coffee_warmed_up_alert

- id: coffee_long_running_alert
  alias: "Telegram bot - кофемашина работает 1 час"
  mode: single
  trigger:
    - platform: state
      entity_id: switch.kofemashina
      to: "on"
      for: "01:00:00"
  condition:
    - condition: state
      entity_id: switch.kofemashina
      state: "on"
  action:
    - action: rest_command.tg_coffee_long_running_alert

- id: ask_sonya_about_tea
  alias: "Спросить Соню про чай"
  mode: single
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set text = trigger.event.data.text | default('') | lower %}
        {{ 'сон' in text and 'чай' in text }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_tea_wants
          - input_boolean.tg_awaiting_sonya_tea_keep_warm
          - input_boolean.tg_awaiting_sonya_tea_keep_warm_temperature
          - input_boolean.sonya_direct_awaiting_tea_keep_warm
          - input_boolean.sonya_direct_awaiting_tea_keep_warm_temperature
          - input_boolean.hall_awaiting_sonya_tea_wants
          - input_boolean.hall_awaiting_sonya_tea_keep_warm
          - input_boolean.hall_awaiting_sonya_tea_keep_warm_temperature
    - action: rest_command.tg_sonya_hall_tea_request

- id: tg_sonya_direct_tea_request
  alias: "Telegram bot - Соня сама просит чай"
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {% set any_active =
          is_state('input_boolean.tg_awaiting_sonya_coffee_temperature', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_coffee_syrup', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_coffee_temperature', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_coffee_syrup', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_tea_wants', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_tea_keep_warm', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_tea_keep_warm_temperature', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm_temperature', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_wants', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm_temperature', 'on')
        %}
        {% set intermediate = text in ['да', 'нет', 'не надо', 'не хочу', 'хочу', 'буду', 'сорок', 'пятьдесят', 'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто'] %}
        {{ not is_service_phrase
           and not any_active
           and not intermediate
           and 'чай' in text
           and 'сон' not in text
           and 'спроси сон' not in text
           and 'узнай' not in text
           and dialog not in [
             'tg_ask_sonya_wants_tea',
             'tg_ask_sonya_tea_keep_warm',
             'tg_ask_sonya_tea_keep_warm_temperature',
             'sonya_direct_tea_keep_warm',
             'sonya_direct_tea_keep_warm_temperature',
             'hall_ask_sonya_wants_tea',
             'hall_sonya_tea_keep_warm',
             'hall_sonya_tea_keep_warm_temperature'
           ] }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_tea_wants
          - input_boolean.tg_awaiting_sonya_tea_keep_warm
          - input_boolean.tg_awaiting_sonya_tea_keep_warm_temperature
          - input_boolean.sonya_direct_awaiting_tea_keep_warm
          - input_boolean.sonya_direct_awaiting_tea_keep_warm_temperature
          - input_boolean.hall_awaiting_sonya_tea_wants
          - input_boolean.hall_awaiting_sonya_tea_keep_warm
          - input_boolean.hall_awaiting_sonya_tea_keep_warm_temperature
    - action: rest_command.tg_sonya_direct_tea_request

- id: tg_sonya_wants_tea_answer
  alias: "Telegram bot - ответ Сони хочет ли чай"
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {{ not is_service_phrase
           and (
             dialog in ['tg_ask_sonya_wants_tea', 'hall_ask_sonya_wants_tea']
             or (
               is_state('input_boolean.tg_awaiting_sonya_tea_wants', 'on')
               or is_state('input_boolean.hall_awaiting_sonya_tea_wants', 'on')
             )
           ) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_tea_wants
          - input_boolean.hall_awaiting_sonya_tea_wants
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: >-
          {% set session = trigger.event.data.session | default({}) %}
          {{ session.dialog | default('tg_ask_sonya_wants_tea') }}
    - action: rest_command.tg_sonya_wants_tea_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_tea_keep_warm_answer
  alias: "Telegram bot - ответ Сони поддержание тепла для чая"
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {{ not is_service_phrase
           and (
             dialog in [
               'tg_ask_sonya_tea_keep_warm',
               'sonya_direct_tea_keep_warm',
               'hall_sonya_tea_keep_warm'
             ]
             or (
               is_state('input_boolean.tg_awaiting_sonya_tea_keep_warm', 'on')
               or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm', 'on')
               or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm', 'on')
             )
           ) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_tea_keep_warm
          - input_boolean.sonya_direct_awaiting_tea_keep_warm
          - input_boolean.hall_awaiting_sonya_tea_keep_warm
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: >-
          {% set session = trigger.event.data.session | default({}) %}
          {{ session.dialog | default('tg_ask_sonya_tea_keep_warm') }}
    - action: rest_command.tg_sonya_tea_keep_warm_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_tea_keep_warm_temperature_answer
  alias: "Telegram bot - ответ Сони температура поддержания тепла для чая"
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
        {% set yandex_dialog_skill_name = 'домашний помощник' %}
        {% set is_service_phrase = 'скажи навыку' in text or yandex_dialog_skill_name in text %}
        {% set temperature_words = ['40', '50', '60', '70', '80', '90', 'сорок', 'пятьдесят', 'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто'] %}
        {% set has_temperature = temperature_words | select('in', text) | list | count > 0 %}
        {{ not is_service_phrase
           and (
             dialog in [
               'tg_ask_sonya_tea_keep_warm_temperature',
               'sonya_direct_tea_keep_warm_temperature',
               'hall_sonya_tea_keep_warm_temperature'
             ]
             or (
               has_temperature and (
                 is_state('input_boolean.tg_awaiting_sonya_tea_keep_warm_temperature', 'on')
                 or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm_temperature', 'on')
                 or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm_temperature', 'on')
               )
             )
           ) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_tea_keep_warm_temperature
          - input_boolean.sonya_direct_awaiting_tea_keep_warm_temperature
          - input_boolean.hall_awaiting_sonya_tea_keep_warm_temperature
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: >-
          {% set session = trigger.event.data.session | default({}) %}
          {{ session.dialog | default('tg_ask_sonya_tea_keep_warm_temperature') }}
    - action: rest_command.tg_sonya_tea_keep_warm_temperature_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"
```

## Tea And Kettle Reference

This section is reference only. Do not copy YAML from here; the ready-to-copy Home Assistant YAML lives in the two sections above.

Kettle entities:

- `water_heater.chainik`
- `switch.chainik_podderzhanie_tepla`
- `switch.chainik_podsvetka`
- `switch.chainik_bez_zvuka`

Kettle rules:

- Boil with `water_heater.set_temperature` and `temperature: 100`.
- Stop with `water_heater.set_operation_mode` and `operation_mode: "off"`.
- Do not use `water_heater.turn_on` or `water_heater.turn_off`.
- Keep-warm uses numeric temperatures only: `40`, `50`, `60`, `70`, `80`, `90`.
- To enable keep-warm: first `water_heater.set_temperature`, then `switch.turn_on switch.chainik_podderzhanie_tepla`.
- `switch.chainik_blokirovka_upravleniia` is intentionally not exposed in Telegram.

Tea internal endpoints:

- `/internal/tea/sonya-wants-answer`
- `/internal/tea/sonya-keep-warm-answer`
- `/internal/tea/sonya-keep-warm-temperature-answer`
- `/internal/tea/sonya-direct-request`
- `/internal/tea/hall-request`
- `/internal/tea/sonya-auto-enabled`
- `/internal/tea/sonya-hall-refused`

Testing checklist:

- Sonya Telegram tea order does not speak in bedroom.
- Direct voice tea asks keep-warm first and does not start kettle before Artem confirms.
- Telegram ask-Sonya tea shows confirmation with `Да / Нет / Попозже`.
- Reminder `Да` starts kettle without bedroom TTS.
- Manual kettle stop uses state-aware stop.

## Useful Maintenance Commands

```bash
docker compose exec homeassistant python -m homeassistant --script check_config --config /config
docker compose restart homeassistant
docker compose restart telegram-bot
docker compose logs --tail=100 telegram-bot
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
