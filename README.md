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

- Home Assistant only reports `switch.kofemashina` state changes to the bot: `on` and `off`.
- HA helpers own warm-up and long-running policy; the bot schedules notification tasks from the current confirmed revision.
- Each coffee alert can be delivered through Telegram, HA Mobile App push via Home Assistant, or both.
- HA Mobile push uses `HA_MOBILE_NOTIFY_SERVICES` such as
  `notify.mobile_app_aaliv_iphone,notify.mobile_app_macbook`. If it is empty, the bot falls back to
  `HA_MOBILE_NOTIFY_SERVICE` for backward compatibility.
- Coffee alert channel settings and per-channel delivery flags are stored in `APP_STATE_PATH`.
- One-time bootstrap defaults are 13 minutes warm-up and 60 minutes for the “works too long” warning. Current values can differ and are refreshed from HA.
- In Telegram, open `Умные устройства` -> `Кофемашина` -> `Настройки` to configure each alert separately:
  enable/disable it and change its delay.
- Alert channel settings and current coffee cycle state are stored in `APP_STATE_PATH`; timing values are canonical in HA helpers.
- If the bot restarts while the stored coffee state is `on`, it restores timers using elapsed time; due unsent alerts are sent immediately.
- If the coffee machine turns off, active coffee alert timers are cancelled and no alert is sent.
- Coffee machine status shows continuous running time while the switch is on; it shows a dash when the switch is off.
- Optional PushWard Live Activity can mirror the coffee cycle on iPhone. Enable it with
  `PUSHWARD_COFFEE_ACTIVITY_ENABLED=true` after installing and testing PushWard in Home Assistant.
- PushWard uses slug `ha-coffee-machine` by default, updates every 30 seconds while warming up,
  stays visible after warm-up, and is ended with `pushward.end_activity` when the coffee machine turns off.
- PushWard progress is always `0.0`-`1.0`. During warm-up the color changes by steps:
  blue `#0A84FF`, cyan `#00AEEF`, teal `#00C7A3`, lime `#A6D94A`, then green `#34C759`
  only when warm-up reaches 100%.
- PushWard time display is configured in Telegram: `Умные устройства` -> `Кофемашина` ->
  `Настройки` -> `PushWard Live Activity`. Default is minutes and seconds. The `Только минуты`
  mode rounds remaining warm-up time up and shows `меньше 1 мин` when less than a minute remains.
- After warm-up the activity stays at progress `1.0` and the color moves by steps from
  green `#34C759` to yellow-green `#C9D94A`, orange `#FF9F0A`, red-orange `#FF6B00`,
  and red `#FF3B30` at the long-running threshold. The icon is `cup.and.saucer` before
  the threshold and `exclamationmark.triangle` when the coffee machine works too long.
- PushWard errors never send Telegram/iPhone user notifications and do not block coffee alerts.
  They are written to `PUSHWARD_ERROR_LOG_PATH`, default `/app/data/pushward_errors.log`.

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

If `turn_on` is called while the coffee machine is already on, the endpoint does not call
`switch.turn_on`, does not restart PushWard or coffee timers, and returns:

```json
{
  "ok": true,
  "action": "turn_on",
  "status": "already_on",
  "message": "РљРѕС„РµРјР°С€РёРЅР° СѓР¶Рµ РІРєР»СЋС‡РµРЅР°. Р’СЂРµРјСЏ СЂР°Р±РѕС‚С‹: 05:23"
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

### Reminders

- Main admin menu has `Напоминания`.
- `Создать напоминание` waits for text. You can write the reminder and delay in one message:
  `напомни убрать посуду через 10 минут`.
- If the message has no delay, the bot asks `Через сколько напомнить?`; answer with `через 10 минут`
  or `через 2 часа`.
- `Управление напоминаниями` lists pending reminders and provides a delete button for each one.
- Fired reminders are sent only to `TELEGRAM_ADMIN_CHAT_ID` and are not voiced on Yandex Stations.
- Reminder storage is persistent JSON at `REMINDERS_STATE_PATH`. Pending reminders are restored after
  bot restart; overdue reminders are sent after startup.
- Supported relative delays: `через X минут/минуту/минуты`, `через X часов/час/часа`,
  `через час`, `через полчаса`, `через пол часа`.
- Delay limits: minimum 1 minute, maximum 24 hours.
- Home Assistant/Alice can create reminders through internal endpoint:
  `POST http://telegram-bot:8088/internal/reminders/alice-create`.
- The current `yandex_intent` event data does not include the source station. Voice replies for
  reminders therefore use persistent bot settings instead of source auto-detection.
- Home Assistant `yandex_intent` text already contains the cleaned command without invocation phrases
  like `скажи навыку` or `попроси домашнего помощника`, so the reminder automation matches the actual
  reminder text: `напом...` plus `через...`.
- Default reminder voice settings: enabled, station `media_player.stantsiia_mini_zal`.
- You can change reminder voice settings in Telegram: `Напоминания` -> `Настройки`.
- Available reminder voice stations:
  `Зал = media_player.stantsiia_mini_zal`,
  `Спальня = media_player.stantsiia_mini_spalnia`.
- Turn voice off in `Напоминания` -> `Настройки` if Alice should not speak reminder confirmations.

Example internal request:

```bash
curl -X POST "http://telegram-bot:8088/internal/reminders/alice-create" \
  -H "X-Internal-Secret: $INTERNAL_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"напомнить, что надо убрать посуду через 10 минут","dialog":"alice_reminder_create","intent":""}'
```

Successful internal response:

```json
{
  "ok": true,
  "message": "Поняла, отправлю напоминание через 10 минут",
  "reminder_id": "abc123",
  "delay_text": "10 минут",
  "voice_enabled": true,
  "voice_station_entity_id": "media_player.stantsiia_mini_zal"
}
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
REMINDERS_STATE_PATH=/app/data/reminders.json

# HTTP proxy for Telegram API requests only.
TELEGRAM_PROXY=login:pass@host:port

# Home Assistant
HA_URL=http://homeassistant:8123
HA_LONG_LIVED_TOKEN=
HA_MOBILE_NOTIFY_SERVICES=notify.mobile_app_aaliv_iphone,notify.mobile_app_macbook
HA_MOBILE_NOTIFY_SERVICE=notify.mobile_app_aaliv_iphone
COFFEE_WARMUP_GIF_URL=https://ha.myhomeassistantisverybest.art/shortcut/assets/coffee.gif
PUSHWARD_COFFEE_ACTIVITY_ENABLED=false
PUSHWARD_COFFEE_ACTIVITY_SLUG=ha-coffee-machine
PUSHWARD_ERROR_LOG_PATH=/app/data/pushward_errors.log
PUSHWARD_COFFEE_ENDED_TTL_SECONDS=3
PUSHWARD_COFFEE_OFF_HOLD_SECONDS=5
PUSHWARD_COFFEE_WIDGET_ENABLED=false
PUSHWARD_INTEGRATION_KEY=
PUSHWARD_COFFEE_WIDGET_SLUG=ha-coffee-machine-widget
PUSHWARD_COFFEE_WIDGET_NAME=Кофемашина
PUSHWARD_COFFEE_WIDGET_UPDATE_INTERVAL_SECONDS=60
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

`APP_STATE_PATH` stores persistent bot UI settings. Currently it stores coffee alert toggles,
coffee alert delays, and the current coffee notification cycle state (`coffee_on_since`, sent flags)
from `Умные устройства` -> `Кофемашина` -> `Настройки`. Mount `/app/data` as a Docker volume if the
container is recreated, not only restarted.

`REMINDERS_STATE_PATH` stores persistent user reminders. Pending reminders are restored after a
telegram-bot container restart; overdue reminders are sent after startup.

`HA_MOBILE_NOTIFY_SERVICES` enables HA Mobile App push notifications for coffee machine alerts through
the Home Assistant Companion App. Set it to a comma-separated list, for example
`notify.mobile_app_aaliv_iphone,notify.mobile_app_macbook`. If it is empty, the bot uses
`HA_MOBILE_NOTIFY_SERVICE` as a backward-compatible fallback. Find services in Home Assistant under
Developer Tools -> Actions. Coffee alert channels are configured in Telegram:
`Умные устройства` -> `Кофемашина` -> `Настройки` -> `Разогрев` / `Долгая работа` -> `Telegram` / `iPhone`.

Coffee timing values now live in canonical Home Assistant helpers. See
[`docs/control-center-integration.md`](docs/control-center-integration.md) for
the migration and sanitized health contracts.
Reminder notification channels are configured in Telegram: `Напоминания` -> `Настройки` -> `Telegram` / `iPhone`.
Coffee push title is `Кофемашина`; reminder push title is `Напоминание`, and the push body is only the reminder text.
Coffee HA Mobile alert pushes use tag `coffee_machine_alert` and include the `COFFEE_TURN_OFF`
action. The HA automation for that action sends a separate short `Кофемашина выключена`
notification with tag `coffee_machine_status_done`; normal Telegram/Siri/HA turn-off paths do not send it.
Shortcut `turn_on` sends a short HA Mobile App status push with tag `coffee_machine_shortcut_status`:
`☕ Кофемашина включена`, or `☕ Кофемашина уже включена. Время работы: MM:SS`
when the switch is already on. The bot clears this status notification after 4 seconds.
The warm-up iPhone alert can include `notification-assets/coffee.gif`. Expose it as
`/shortcut/assets/coffee.gif` and set `COFFEE_WARMUP_GIF_URL` to the public URL. If the variable
is empty, the warm-up push is sent without GIF. Coffee pushes use only standard Unicode emoji;
custom Telegram emoji are not used in Home Assistant notifications.
If both `HA_MOBILE_NOTIFY_SERVICES` and `HA_MOBILE_NOTIFY_SERVICE` are empty, HA Mobile push is skipped
and Telegram notifications keep working.

`PUSHWARD_COFFEE_ACTIVITY_ENABLED` controls the optional PushWard Live Activity for the coffee machine.
Default is `false`, so installations without PushWard keep working unchanged. To enable it, install the
PushWard HACS integration in Home Assistant, test `pushward.create_activity`, `pushward.update_activity_generic`
or `pushward.update_activity`, and `pushward.end_activity` in Developer Tools -> Actions, then set
`PUSHWARD_COFFEE_ACTIVITY_ENABLED=true`. The activity uses `PUSHWARD_COFFEE_ACTIVITY_SLUG`
(`ha-coffee-machine` by default). PushWard service errors are written to
`PUSHWARD_ERROR_LOG_PATH` and are intentionally not shown to Telegram or iPhone users.
For updates, the bot prefers `pushward.update_activity_generic` when Home Assistant advertises it;
otherwise it falls back to deprecated `pushward.update_activity` with `template: generic`. The top-level
PushWard activity `state` sent through Home Assistant is always lowercase: `ongoing`.
Coffee PushWard activities are created with `PUSHWARD_COFFEE_ENDED_TTL_SECONDS` (`3` by default).
On coffee machine `off`, the bot first updates Live Activity to `Кофемашина выключена`, holds it for
`PUSHWARD_COFFEE_OFF_HOLD_SECONDS` (`5` by default), then calls `pushward.end_activity`. A delayed
`delete_activity` is only best-effort cleanup, not the main completion path.

PushWard off cleanup timing can be tested without switching the real coffee machine:

```bash
python scripts/test_pushward_coffee_off_cleanup.py --hold-seconds 5 --ended-ttl 3
python scripts/test_pushward_coffee_off_cleanup.py --hold-seconds 4 --ended-ttl 3
python scripts/test_pushward_coffee_off_cleanup.py --hold-seconds 5 --ended-ttl 2
```

The script reads `HA_URL` and `HA_LONG_LIVED_TOKEN` from env, creates a test activity, shows
`Кофемашина разогрета`, switches it to `Кофемашина выключена`, waits `hold_seconds`, then calls
`end_activity`. Add `--cleanup` to send a best-effort `delete_activity` after the end.

### PushWard Coffee Widget

The coffee widget is optional and uses the direct PushWard API, not Home Assistant
`pushward.widget_refresh`. It is disabled by default:

```env
PUSHWARD_COFFEE_WIDGET_ENABLED=true
PUSHWARD_INTEGRATION_KEY=<integration key>
PUSHWARD_COFFEE_WIDGET_SLUG=ha-coffee-machine-widget
PUSHWARD_COFFEE_WIDGET_NAME=Кофемашина
PUSHWARD_COFFEE_WIDGET_UPDATE_INTERVAL_SECONDS=60
```

The bot updates `PATCH https://api.pushward.app/widgets/{slug}` with template `stat_list`.
If PATCH returns `404`, the bot bootstraps the widget once with `POST /widgets` using the same
content. The integration key is read only from env and is never logged.

Widget content has two rows: `Статус` (`Вкл` / `Выкл`) and `Работает` (`—`, `меньше 1 мин`,
`7 мин`, or `1 ч 07 мин`). It updates immediately on coffee machine `on`/`off` and then every
`PUSHWARD_COFFEE_WIDGET_UPDATE_INTERVAL_SECONDS` while the coffee machine is on. Repeated
`state=on` events do not create parallel loops.

Colors: off `#8E8E93`, warming `#0A84FF` -> `#00AEEF` -> `#00C7A3`, ready `#34C759`,
almost long-running `#FF9F0A`, too long `#FF3B30`.

After bootstrap, add the widget in PushWard by slug `ha-coffee-machine-widget`. Recommended
placements: Lock Screen Rectangular, or Home Screen Medium/Large. Widget errors are written to
`PUSHWARD_ERROR_LOG_PATH` and do not block `/internal/coffee-machine/state`, Live Activity,
Telegram alerts, HA Mobile pushes, or coffee timers.

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

Caddy should expose only the public shortcut endpoint from the bot and route everything else to Home Assistant:

```caddyfile
ha.myhomeassistantisverybest.art {
	handle /shortcut/espresso {
		reverse_proxy telegram-bot:8088
	}

	handle /shortcut/assets/coffee.gif {
		reverse_proxy telegram-bot:8088
	}

	handle {
		reverse_proxy homeassistant:8123
	}
}
```

Do not proxy `/internal/*` or all bot paths to the public internet. Home Assistant should keep using
the internal Docker URL `http://telegram-bot:8088/internal/...`.

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
- `coffee_machine_state_to_telegram_bot`
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

  tg_coffee_machine_state:
    url: "http://telegram-bot:8088/internal/coffee-machine/state"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"entity_id": {{ entity_id | to_json }}, "state": {{ state | to_json }}, "changed_at": {{ changed_at | to_json }}}

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

  tg_alice_reminder_create:
    url: "http://telegram-bot:8088/internal/reminders/alice-create"
    method: POST
    content_type: "application/json"
    headers:
      Content-Type: "application/json"
      X-Internal-Secret: !secret internal_webhook_secret
    payload: >-
      {"text": {{ text | to_json }}, "intent": {{ intent | to_json }}, "dialog": {{ dialog | to_json }}}
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
- id: coffee_machine_state_to_telegram_bot
  alias: "Telegram bot - состояние кофемашины"
  mode: single
  trigger:
    - platform: state
      entity_id: switch.kofemashina
      to:
        - "on"
        - "off"
  action:
    - action: rest_command.tg_coffee_machine_state
      data:
        entity_id: "{{ trigger.entity_id }}"
        state: "{{ trigger.to_state.state }}"
        changed_at: "{{ trigger.to_state.last_changed.isoformat() }}"

- id: iphone_coffee_notification_turn_off
  alias: "iPhone уведомление - выключить кофемашину"
  mode: restart
  trigger:
    - platform: event
      event_type: mobile_app_notification_action
      event_data:
        action: "COFFEE_TURN_OFF"
  action:
    - service: switch.turn_off
      target:
        entity_id: switch.kofemashina

    - service: notify.mobile_app_aaliv_iphone
      data:
        message: "clear_notification"
        data:
          tag: "coffee_machine_alert"

    - service: notify.mobile_app_aaliv_iphone
      data:
        title: "Кофемашина"
        message: "✅ Кофемашина выключена"
        data:
          tag: "coffee_machine_status_done"

    - delay:
        seconds: 4

    - service: notify.mobile_app_aaliv_iphone
      data:
        message: "clear_notification"
        data:
          tag: "coffee_machine_status_done"

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

- id: tg_alice_reminder_create
  alias: "Telegram bot - создать напоминание через Алису"
  mode: queued
  trigger:
    - platform: event
      event_type: yandex_intent
  condition:
    - condition: template
      value_template: >-
        {% set text = trigger.event.data.text | default('') | lower %}
        {% set session = trigger.event.data.session | default({}) %}
        {% set dialog = session.dialog | default('') %}
        {{ 'напом' in text and 'через' in text and dialog not in [
             'tg_ask_sonya_wants_coffee',
             'tg_ask_sonya_coffee_temperature',
             'tg_ask_sonya_coffee_syrup',
             'tg_ask_sonya_coffee_comment',
             'tg_ask_sonya_wants_tea',
             'tg_ask_sonya_tea_keep_warm',
             'tg_ask_sonya_tea_comment',
             'tg_ask_sonya_wants_water',
             'tg_ask_sonya_water_comment'
           ] }}
  action:
    - variables:
        text: "{{ trigger.event.data.text | default('') }}"
        intent: "{{ trigger.event.data.intent | default('') }}"
        session: "{{ trigger.event.data.session | default({}) }}"
        dialog: "{{ session.dialog | default('alice_reminder_create') }}"
    - action: rest_command.tg_alice_reminder_create
      response_variable: reminder_response
      data:
        text: "{{ text }}"
        intent: "{{ intent }}"
        dialog: "{{ dialog }}"
    - variables:
        reminder_voice_enabled: "{{ reminder_response.content.voice_enabled | default(true) }}"
        reminder_voice_station_entity: "{{ reminder_response.content.voice_station_entity_id | default('media_player.stantsiia_mini_zal') }}"
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ reminder_voice_enabled | bool }}"
          sequence:
            - action: media_player.play_media
              target:
                entity_id: "{{ reminder_voice_station_entity }}"
              data:
                media_content_id: "{{ reminder_response.content.message | default('Не поняла, через сколько напомнить') }}"
                media_content_type: "text"
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

Coffee alert settings are not reminder tasks. Coffee alert toggles, custom delays, and current
coffee cycle state are stored separately in the JSON file configured by `APP_STATE_PATH`, so normal
container restarts keep the settings and restore active alert timers when the stored state is `on`.
