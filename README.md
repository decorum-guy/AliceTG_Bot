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
- `Озвучить`
- `Разговор`
- `Настройки`

`Умные устройства` contains:

- `Кофемашина`
- `Чайник`
- `Назад`

### Sonya Menu

Sonya sees only her own order menu:

- `☕️ Кофе`
- `🍵 Чай`

Sonya does not see `Спросить Соню`, `Умные устройства`, direct coffee machine control, or direct kettle control.

### Admin Voice Modes

- `Озвучить` is available only in Artem's main menu. Artem chooses `Зал` or `Спальня`, chooses volume from `0.0` to `1.0`, then every text message is spoken on the selected speaker as plain TTS.
- `Разговор` is available only in Artem's main menu. Artem chooses `Зал` or `Спальня`, chooses volume, then each text message is spoken through a Yandex dialog tag: `admin_talk_zal` or `admin_talk_spalnia`.
- In `Разговор`, if the station returns a `yandex_intent` answer for one of these two dialog tags, Telegram receives `Соня сказала: <answer>`.
- In `Озвучить` and `Разговор`, prefix one message with `/шепот/ текст` or `/шёпот/ текст` to speak only that message in whisper mode. The next message is normal unless it also has the prefix.
- Whisper in `Разговор` uses the same dialog tag and adds Yandex speaker markup to `media_content_id`, so the station should still listen for Sonya's answer after the whispered phrase.
- `/stop` exits either admin mode, restores the speaker volume if the previous volume was available, and shows the normal admin menu.
- If an active `Озвучить` or `Разговор` message ends with `/stop`, the bot sends the text before `/stop` first, then exits the mode and restores the speaker volume. A message containing only `/stop` exits without speaking.
- Non-text messages in an active admin mode get `Отправь текст или /stop.`.

### Admin Settings

- `Настройки` is available only in Artem's main menu.
- `Сбросить флаги и режимы` turns off only Sonya coffee/tea waiting `input_boolean` flags and clears Artem's active `Озвучить` or `Разговор` session.
- If an active admin mode stored the previous speaker volume, the bot tries to restore it before clearing the session.
- The reset does not turn off the coffee machine, kettle, sockets, kettle light, mute mode, or keep-warm mode.

### Coffee Workflow

- Telegram-initiated coffee flow may edit the active Telegram message after each step.
- Direct voice coffee flow does not send an intermediate Telegram message after temperature. It creates the Telegram confirmation only after syrup is received.
- Hall/zal voice flow is separate from direct voice flow. It asks whether Sonya wants coffee first, collects the order, and sends Artem a Telegram confirmation. It does not turn on the coffee machine from Sonya's voice answer.
- Voice-based coffee flows say one short bedroom acknowledgement after Sonya gives the final answer: `Хорошо, заказ принят.`.
- Coffee orders now ask one extra voice question after temperature and syrup: `Есть пожелания?`.
  Send the answer to `/internal/coffee/sonya-comment-answer` with dialog `tg_ask_sonya_coffee_comment`
  or `sonya_direct_coffee_comment`. Legacy HA hall flows that still call
  `/internal/coffee/sonya-auto-enabled` directly can pass optional JSON `comment`;
  the bot now treats that endpoint as a Telegram confirmation request, not as permission
  to turn on the coffee machine. Otherwise Telegram shows `Комментарий: -`.
- After Artem presses `Да`, `Нет`, or `Попозже` in Telegram, the bedroom does not say anything. Telegram messages and device actions continue normally.
- Before Artem confirms in Telegram, the coffee machine does not turn on.
- If an internal coffee event fails while being processed, the bot logs `Coffee workflow failed, resetting coffee flags` and resets all coffee wait flags through Home Assistant.

### Coffee Alerts

- If `switch.kofemashina` stays on continuously for 13 minutes, Telegram receives a warmed-up alert with a `Выключить` button.
- If `switch.kofemashina` turns off before 13 minutes, the warmed-up alert is not sent.
- If `switch.kofemashina` stays on continuously for 1 hour, Telegram receives a warning alert with a `Выключить` button.
- In Telegram, open `Умные устройства` -> `Кофемашина` -> `Настройки` to enable or disable coffee alerts separately:
  `Уведомление о готовности 13 мин` and `Уведомление о перегреве 1 час`.
- The alert settings are stored in `APP_STATE_PATH` and survive bot container restarts.
- Coffee machine status shows continuous running time while the switch is on; it shows a dash when the switch is off.

### Siri And Telegram Coffee Control

- Telegram admin commands:
  - `/coffee_on` turns on `switch.kofemashina`.
  - `/coffee_off` turns off `switch.kofemashina`.
- Both commands are admin-only and use the same internal coffee action as the HTTP shortcut endpoint.
- iPhone Shortcuts can call `POST /shortcut/espresso` on the bot HTTP server.
- The shortcut endpoint requires `Authorization: Bearer <SHORTCUTS_SECRET_TOKEN>`.
- If `SHORTCUTS_SECRET_TOKEN` is empty, the endpoint returns `503` and does not perform any action.
- The endpoint accepts JSON body only:

```json
{"action": "turn_on"}
```

or:

```json
{"action": "turn_off"}
```

Example `curl`:

```bash
curl -X POST "https://your-bot-domain.example/shortcut/espresso" \
  -H "Authorization: Bearer $SHORTCUTS_SECRET_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"turn_on"}'
```

Successful response:

```json
{
  "ok": true,
  "action": "turn_on",
  "message": "Кофемашина включена"
}
```

Successful `turn_off` response:

```json
{
  "ok": true,
  "action": "turn_off",
  "message": "Кофемашина выключена"
}
```

Shortcut-safe error responses:

```json
{"ok": false, "error": "unauthorized", "message": "Команда отклонена: неверный токен"}
{"ok": false, "error": "invalid_action", "message": "Неизвестная команда"}
{"ok": false, "error": "home_assistant_error", "message": "Не удалось выполнить команду для кофемашины"}
```

iPhone Shortcut setup:

1. Create a shortcut named `I want coffee` or `turn on espresso machine`.
2. Add `Get Contents of URL`.
3. URL: `https://<domain-or-ip>/shortcut/espresso`.
4. Method: `POST`.
5. Headers:
   `Authorization: Bearer <SHORTCUTS_SECRET_TOKEN>`
   `Content-Type: application/json`
6. Request body:

```json
{"action": "turn_on"}
```

7. Add `Get Dictionary Value` with key `message`.
8. Add `Show Notification` or `Speak Text`.

For a second shortcut named `turn off espresso machine`, use the same setup with this body:

```json
{"action": "turn_off"}
```

### Tea And Kettle Workflow

- Telegram ask-Sonya tea flow asks whether Sonya wants tea, then asks about keep-warm.
- Direct voice tea flow asks keep-warm first and does not start the kettle before Artem confirms.
- Hall/zal tea flow asks whether Sonya wants tea first, collects keep-warm and comment answers, and sends Artem a Telegram confirmation. It does not start the kettle from Sonya's voice answer.
- Voice-based tea flows say one short bedroom acknowledgement after Sonya gives the final order answer: `Хорошо, заказ принят.`.
- Tea orders now ask one extra voice question after keep-warm settings: `Есть пожелания?`.
  Send the answer to `/internal/tea/sonya-comment-answer` with dialog `tg_ask_sonya_tea_comment`,
  `sonya_direct_tea_comment`, or `hall_ask_sonya_tea_comment`. Legacy HA hall flows that
  still call `/internal/tea/sonya-auto-enabled` directly can pass optional JSON `comment`;
  that endpoint sends Telegram info only and does not start the kettle. Otherwise Telegram
  shows `Комментарий: -`.
- After Artem presses `Да`, `Нет`, or `Попозже` in Telegram, the bedroom does not say anything. Telegram messages, kettle start, and post-boil keep-warm continue normally.
- Sonya Telegram tea order does not speak in the bedroom.
- Boil with `water_heater.set_temperature` and `temperature: 100`.
- Stop with `water_heater.set_operation_mode` and `operation_mode: "off"`.
- Do not use `water_heater.turn_on` or `water_heater.turn_off`.
- Keep-warm uses numeric temperatures only: `40`, `50`, `60`, `70`, `80`, `90`.
- To enable keep-warm, first call `water_heater.set_temperature`, then `switch.turn_on switch.chainik_podderzhanie_tepla`.
- Kettle light is `switch.chainik_podsvetka`.
- Kettle mute mode is `switch.chainik_bez_zvuka`.

### Water Workflow

- Sonya's Telegram menu includes `Вода`.
- Artem's `Спросить Соню` menu includes `Хочет ли воды?`; this asks Sonya through the bedroom station and then asks `Есть пожелания?`.
- Water orders do not use the coffee/tea `Да / Нет / Попозже` device confirmation. Telegram shows `Сейчас` and `Попозже`.
- `Сейчас` says in the bedroom: `Артём скоро принесёт воду.` No Home Assistant device is switched.
- `Попозже` offers 1-5 minutes, says in the bedroom: `Артём принесёт воду через X минут.`, and sends a final Telegram reminder with only `Удалить уведомление`.
- New water internal endpoints:
  - `/internal/water/sonya-wants-answer`
  - `/internal/water/sonya-comment-answer`
  - `/internal/water/sonya-direct-request`
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
APP_STATE_PATH=/app/data/state.json

# HTTP proxy for Telegram API requests only.
TELEGRAM_PROXY=login:pass@host:port

# Home Assistant
HA_URL=http://homeassistant:8123
HA_LONG_LIVED_TOKEN=
YANDEX_DIALOG_SKILL_NAME=домашний помощник

# Internal webhook security between Home Assistant and bot
INTERNAL_WEBHOOK_SECRET=

# iPhone Shortcuts / Siri HTTP endpoint security
SHORTCUTS_SECRET_TOKEN=change_me
```

`TELEGRAM_ALLOWED_USER_IDS` accepts comma-separated IDs, for example:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

`TELEGRAM_SONYA_USER_IDS` is optional. If it is set, those users see only the order menu:

```env
TELEGRAM_SONYA_USER_IDS=222222222
```

`APP_STATE_PATH` stores persistent bot UI settings. Currently it stores the separate coffee alert
toggles from `Умные устройства` -> `Кофемашина` -> `Настройки`: 13-minute readiness and 1-hour
overheat warnings. Mount `/app/data` as a Docker volume if the container is recreated, not only restarted.

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

Current recommended internal coffee endpoints:

```text
http://telegram-bot:8088/internal/coffee/sonya-wants-answer
http://telegram-bot:8088/internal/coffee/sonya-temperature-answer
http://telegram-bot:8088/internal/coffee/sonya-syrup-answer
```

Direct voice coffee flow uses the same recommended endpoints. The bot detects direct flow by `dialog`:

- `sonya_direct_coffee_temperature`
- `sonya_direct_coffee_syrup`

Legacy compatibility endpoints still exist in the code:

- `/internal/coffee/sonya-type-answer`
- `/internal/coffee/sonya-direct-type-answer`

Use them only for old integrations. New Home Assistant YAML should use `/sonya-temperature-answer`
and `/sonya-syrup-answer`. The legacy endpoints handle only the old type/temperature step and do
not replace syrup handling.

## Home Assistant configuration.yaml

Use this section as the source of truth for Home Assistant `configuration.yaml`. Keep secrets in `secrets.yaml`; do not paste secret values into git.

The active automation IDs are:

- `ask_sonya_about_coffee`
- `tg_sonya_wants_coffee_answer`
- `tg_sonya_temperature_answer`
- `tg_sonya_syrup_answer`
- `tg_sonya_coffee_comment_answer`
- `tg_sonya_direct_coffee_request`
- `tg_sonya_direct_temperature_answer`
- `tg_sonya_direct_syrup_answer`
- `coffee_warmed_up_alert`
- `coffee_long_running_alert`
- `admin_talk_answer`
- `ask_sonya_about_tea`
- `tg_sonya_direct_tea_request`
- `tg_sonya_wants_tea_answer`
- `tg_sonya_tea_keep_warm_answer`
- `tg_sonya_tea_comment_answer`
- `ask_sonya_about_water`
- `tg_sonya_wants_water_answer`
- `tg_sonya_water_comment_answer`
- `tg_sonya_direct_water_request`

Old pre-split coffee/tea automations should be removed before pasting this file so there are no duplicate handlers.

Add to `secrets.yaml`:

```yaml
internal_webhook_secret: "put-the-same-value-as-INTERNAL_WEBHOOK_SECRET"
```

Use this as the full ready-to-copy content of `config/configuration.yaml`:

```yaml
# Loads default set of integrations. Do not remove.
default_config:

# Load frontend themes from the themes folder
frontend:
  themes: !include_dir_merge_named themes

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml

http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 172.16.0.0/12

input_boolean:
  tg_awaiting_sonya_coffee_temperature:
    name: Telegram awaiting Sonya coffee temperature
  tg_awaiting_sonya_coffee_syrup:
    name: Telegram awaiting Sonya coffee syrup
  tg_awaiting_sonya_coffee_comment:
    name: Telegram awaiting Sonya coffee comment
  sonya_direct_awaiting_coffee_temperature:
    name: Sonya direct awaiting coffee temperature
  sonya_direct_awaiting_coffee_syrup:
    name: Sonya direct awaiting coffee syrup
  sonya_direct_awaiting_coffee_comment:
    name: Sonya direct awaiting coffee comment
  hall_awaiting_sonya_coffee_temperature:
    name: Hall awaiting Sonya coffee temperature
  hall_awaiting_sonya_coffee_syrup:
    name: Hall awaiting Sonya coffee syrup
  hall_awaiting_sonya_coffee_comment:
    name: Hall awaiting Sonya coffee comment
  tg_awaiting_sonya_tea_wants:
    name: Telegram awaiting Sonya tea wants
  tg_awaiting_sonya_tea_keep_warm:
    name: Telegram awaiting Sonya tea keep warm
  tg_awaiting_sonya_tea_comment:
    name: Telegram awaiting Sonya tea comment
  sonya_direct_awaiting_tea_keep_warm:
    name: Sonya direct awaiting tea keep warm
  sonya_direct_awaiting_tea_comment:
    name: Sonya direct awaiting tea comment
  hall_awaiting_sonya_tea_wants:
    name: Hall awaiting Sonya tea wants
  hall_awaiting_sonya_tea_keep_warm:
    name: Hall awaiting Sonya tea keep warm
  hall_awaiting_sonya_tea_comment:
    name: Hall awaiting Sonya tea comment
  tg_awaiting_sonya_water_wants:
    name: Telegram awaiting Sonya water wants
  tg_awaiting_sonya_water_comment:
    name: Telegram awaiting Sonya water comment
  sonya_direct_awaiting_water_comment:
    name: Sonya direct awaiting water comment

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

  tg_sonya_coffee_comment_answer:
    url: "http://telegram-bot:8088/internal/coffee/sonya-comment-answer"
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
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}, "comment": {{ comment | default('') | to_json }}}

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

  tg_admin_talk_answer:
    url: "http://telegram-bot:8088/internal/admin/talk-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

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

  tg_sonya_tea_comment_answer:
    url: "http://telegram-bot:8088/internal/tea/sonya-comment-answer"
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
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}, "comment": {{ comment | default('') | to_json }}}

  tg_sonya_tea_hall_refused:
    url: "http://telegram-bot:8088/internal/tea/sonya-hall-refused"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: "{}"

  tg_sonya_wants_water_answer:
    url: "http://telegram-bot:8088/internal/water/sonya-wants-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_water_comment_answer:
    url: "http://telegram-bot:8088/internal/water/sonya-comment-answer"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"answer": {{ answer | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}

  tg_sonya_direct_water_request:
    url: "http://telegram-bot:8088/internal/water/sonya-direct-request"
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
                    - action: input_boolean.turn_on
                      target:
                        entity_id: input_boolean.hall_awaiting_sonya_coffee_comment
                    - action: media_player.play_media
                      target:
                        entity_id: media_player.stantsiia_mini_spalnia
                      data:
                        media_content_id: "Есть пожелания?"
                        media_content_type: "dialog:домашний помощник:hall_ask_sonya_coffee_comment"
                    - wait_for_trigger:
                        - platform: event
                          event_type: yandex_intent
                          event_data:
                            session:
                              dialog: hall_ask_sonya_coffee_comment
                      timeout: "00:00:30"
                      continue_on_timeout: true
                    - variables:
                        coffee_comment: >-
                          {% if wait.trigger is none %}
                            -
                          {% else %}
                            {% set raw_comment = wait.trigger.event.data.text | default('') | trim %}
                            {% if (raw_comment | lower).startswith('нет') %}-{% else %}{{ raw_comment }}{% endif %}
                          {% endif %}
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
                          - input_boolean.hall_awaiting_sonya_coffee_comment
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
                        comment: "{{ coffee_comment }}"
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
           or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_wants', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_comment', 'on') %}
        {% set any_active = any_active
           or is_state('input_boolean.tg_awaiting_sonya_coffee_comment', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_coffee_comment', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_coffee_comment', 'on')
           or is_state('input_boolean.tg_awaiting_sonya_tea_comment', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_tea_comment', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_comment', 'on')
           or is_state('input_boolean.tg_awaiting_sonya_water_wants', 'on')
           or is_state('input_boolean.tg_awaiting_sonya_water_comment', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_water_comment', 'on') %}
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

- id: tg_sonya_coffee_comment_answer
  alias: "Telegram bot - ответ Сони комментарий к кофе"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {{ dialog in ['tg_ask_sonya_coffee_comment', 'sonya_direct_coffee_comment']
           or is_state('input_boolean.tg_awaiting_sonya_coffee_comment', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_coffee_comment', 'on') }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_coffee_comment
          - input_boolean.sonya_direct_awaiting_coffee_comment
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: >-
          {% set session = trigger.event.data.session | default({}) %}
          {{ session.dialog | default('tg_ask_sonya_coffee_comment') }}
    - action: rest_command.tg_sonya_coffee_comment_answer
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
      for: "00:13:00"
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

- id: admin_talk_answer
  alias: "Telegram bot - ответ в режиме разговора"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {{ dialog in ['admin_talk_spalnia', 'admin_talk_zal'] }}
  action:
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: >-
          {% set session = trigger.event.data.session | default({}) %}
          {{ session.dialog | default('') }}
    - action: rest_command.tg_admin_talk_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

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
          - input_boolean.sonya_direct_awaiting_tea_keep_warm
          - input_boolean.hall_awaiting_sonya_tea_wants
          - input_boolean.hall_awaiting_sonya_tea_keep_warm
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
          or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_wants', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm', 'on')
        %}
        {% set any_active = any_active
          or is_state('input_boolean.tg_awaiting_sonya_coffee_comment', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_coffee_comment', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_coffee_comment', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_tea_comment', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_tea_comment', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_comment', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_water_wants', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_water_comment', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_water_comment', 'on')
        %}
        {% set intermediate = text in ['да', 'нет', 'не надо', 'не хочу', 'хочу', 'буду'] %}
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
             'sonya_direct_tea_keep_warm',
             'hall_ask_sonya_wants_tea',
             'hall_ask_sonya_tea_keep_warm',
           ] }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_tea_wants
          - input_boolean.tg_awaiting_sonya_tea_keep_warm
          - input_boolean.sonya_direct_awaiting_tea_keep_warm
          - input_boolean.hall_awaiting_sonya_tea_wants
          - input_boolean.hall_awaiting_sonya_tea_keep_warm
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
               'hall_ask_sonya_tea_keep_warm'
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

- id: tg_sonya_tea_comment_answer
  alias: "Telegram bot - ответ Сони комментарий к чаю"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {{ dialog in ['tg_ask_sonya_tea_comment', 'sonya_direct_tea_comment', 'hall_ask_sonya_tea_comment']
           or is_state('input_boolean.tg_awaiting_sonya_tea_comment', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_tea_comment', 'on')
           or is_state('input_boolean.hall_awaiting_sonya_tea_comment', 'on') }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_tea_comment
          - input_boolean.sonya_direct_awaiting_tea_comment
          - input_boolean.hall_awaiting_sonya_tea_comment
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: >-
          {% set session = trigger.event.data.session | default({}) %}
          {{ session.dialog | default('tg_ask_sonya_tea_comment') }}
    - action: rest_command.tg_sonya_tea_comment_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: ask_sonya_about_water
  alias: "Спросить Соню про воду"
  mode: single
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set text = trigger.event.data.text | default('') | lower %}
        {{ 'сон' in text and ('вод' in text or 'воды' in text) }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_water_wants
          - input_boolean.tg_awaiting_sonya_water_comment
          - input_boolean.sonya_direct_awaiting_water_comment
    - action: media_player.play_media
      target:
        entity_id: media_player.stantsiia_mini_spalnia
      data:
        media_content_id: "Соня, тебя спрашивают: хочешь воды?"
        media_content_type: "dialog:домашний помощник:tg_ask_sonya_wants_water"

- id: tg_sonya_wants_water_answer
  alias: "Telegram bot - ответ Сони хочет ли воды"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {{ dialog == 'tg_ask_sonya_wants_water'
           or is_state('input_boolean.tg_awaiting_sonya_water_wants', 'on') }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.tg_awaiting_sonya_water_wants
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        dialog: "tg_ask_sonya_wants_water"
    - action: rest_command.tg_sonya_wants_water_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_water_comment_answer
  alias: "Telegram bot - ответ Сони комментарий к воде"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {{ dialog in ['tg_ask_sonya_water_comment', 'sonya_direct_water_comment']
           or is_state('input_boolean.tg_awaiting_sonya_water_comment', 'on')
           or is_state('input_boolean.sonya_direct_awaiting_water_comment', 'on') }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_water_comment
          - input_boolean.sonya_direct_awaiting_water_comment
    - variables:
        answer: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: >-
          {% set session = trigger.event.data.session | default({}) %}
          {{ session.dialog | default('tg_ask_sonya_water_comment') }}
    - action: rest_command.tg_sonya_water_comment_answer
      data:
        answer: "{{ answer }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"

- id: tg_sonya_direct_water_request
  alias: "Telegram bot - Соня сама просит воды"
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
        {% set any_active =
          is_state('input_boolean.tg_awaiting_sonya_coffee_temperature', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_coffee_syrup', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_coffee_comment', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_coffee_temperature', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_coffee_syrup', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_coffee_comment', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_coffee_temperature', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_coffee_syrup', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_coffee_comment', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_tea_wants', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_tea_keep_warm', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_tea_comment', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_tea_keep_warm', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_tea_comment', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_wants', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_keep_warm', 'on')
          or is_state('input_boolean.hall_awaiting_sonya_tea_comment', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_water_wants', 'on')
          or is_state('input_boolean.tg_awaiting_sonya_water_comment', 'on')
          or is_state('input_boolean.sonya_direct_awaiting_water_comment', 'on')
        %}
        {{ not any_active
           and ('вод' in text or 'воды' in text)
           and 'сон' not in text
           and dialog not in ['tg_ask_sonya_wants_water', 'tg_ask_sonya_water_comment', 'sonya_direct_water_comment'] }}
  action:
    - action: input_boolean.turn_off
      target:
        entity_id:
          - input_boolean.tg_awaiting_sonya_water_wants
          - input_boolean.tg_awaiting_sonya_water_comment
          - input_boolean.sonya_direct_awaiting_water_comment
    - action: rest_command.tg_sonya_direct_water_request
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

Coffee alert settings are not reminder tasks. The 13-minute readiness toggle and the 1-hour
overheat toggle are stored separately in the JSON file configured by `APP_STATE_PATH`, so normal
container restarts keep both settings.
