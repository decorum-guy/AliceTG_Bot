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
