# Station Mini 2 controls (#188)

`STATION_PLAYER_ENTITY` is the server-owned media player target. When omitted it
defaults to `media_player.stantsiia_mini_zal`, preserving the existing living-room
station target. No client can select a different speaker.

Artem's Telegram **🏠 Умные устройства → 🔊 Станция Mini 2** menu exposes the
seven fixed actions. Sonya's menu does not include this control. The private
Control Center endpoint is `POST /internal/control-center/station/action`, using
the existing `CONTROL_CENTER_API_TOKEN` Bearer token and body
`{"action":"next","requestId":"<UUID>"}`.

iPhone Shortcuts can call `POST /shortcut/station` with the existing
`SHORTCUTS_SECRET_TOKEN` Bearer authorization and JSON `{"action":"play"}` or
`{"action":"pause"}`. Those are the only public Shortcut actions. Keep token
values in deployment secrets, never in Shortcut examples or source files.

Each action sends one fixed `media_player.play_media` command with
`media_content_type=command`. A successful response confirms dispatch to Home
Assistant only; cloud playback state is not verified. Transport failures with
unknown delivery return `uncertain` and must not be retried automatically.
Physical Station behavior remains for owner acceptance after Orchestrator review.

## Shared editable music presets

AliceTG_Bot owns the single canonical Station preset inventory in the existing
`APP_STATE_PATH` / `AppStateStore` JSON object. Its persisted fields are
`station_presets` (ordered `{id,title,command}` objects),
`station_presets_revision` (opaque revision), and
`station_presets_updated_at` (UTC timestamp). An absent `station_presets` key
means never initialized: the first read atomically seeds `Избранное` →
`Включи плейлист Мне нравится`, `Спокойная` → `Включи спокойную музыку`, and
`Энергичная` → `Включи энергичную музыку`. A persisted empty array remains
empty after restart. Add and delete require the current revision and use the
same atomic candidate write as other AppState settings.

The private, bearer-token-protected Control Center API exposes a command-free
`GET /internal/control-center/station/presets`, typed add/delete routes under
the same path, and `POST /internal/control-center/station/presets/{id}/execute`.
Inventory contains only ID and title. Configuration add accepts title and
command; execution accepts only UUID request ID in its JSON body and the
preset ID in its path. AliceTG_Bot resolves the command and sends exactly one
request through the existing Station transport. Unknown IDs fail before HA.
There is no generic Alice command execution surface.

Telegram's admin-only `🎵 Музыка` submenu reads the current inventory each
time. `⚙ Подборки` shows title-only entries and delete confirmation. Add asks
for title, then command, then explicit `✅ Сохранить` confirmation; `Отмена`
clears the transient FSM draft. Sonya cannot see, configure, or execute these
presets, including through forged callback data. Callback data holds IDs only.
