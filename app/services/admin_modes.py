from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AdminMode = Literal["announce", "talk"]
AdminRoom = Literal["hall", "bedroom"]

ADMIN_ROOM_OPTIONS: dict[AdminRoom, tuple[str, str]] = {
    "hall": ("Зал", "media_player.stantsiia_mini_zal"),
    "bedroom": ("Спальня", "media_player.stantsiia_mini_spalnia"),
}

ADMIN_TALK_DIALOGS: dict[AdminRoom, str] = {
    "hall": "admin_talk_zal",
    "bedroom": "admin_talk_spalnia",
}

SONYA_WAITING_FLAGS: tuple[str, ...] = (
    "input_boolean.tg_awaiting_sonya_coffee_temperature",
    "input_boolean.tg_awaiting_sonya_coffee_syrup",
    "input_boolean.tg_awaiting_sonya_coffee_comment",
    "input_boolean.sonya_direct_awaiting_coffee_temperature",
    "input_boolean.sonya_direct_awaiting_coffee_syrup",
    "input_boolean.sonya_direct_awaiting_coffee_comment",
    "input_boolean.hall_awaiting_sonya_coffee_temperature",
    "input_boolean.hall_awaiting_sonya_coffee_syrup",
    "input_boolean.hall_awaiting_sonya_coffee_comment",
    "input_boolean.tg_awaiting_sonya_tea_wants",
    "input_boolean.tg_awaiting_sonya_tea_keep_warm",
    "input_boolean.tg_awaiting_sonya_tea_comment",
    "input_boolean.sonya_direct_awaiting_tea_keep_warm",
    "input_boolean.sonya_direct_awaiting_tea_comment",
    "input_boolean.hall_awaiting_sonya_tea_wants",
    "input_boolean.hall_awaiting_sonya_tea_keep_warm",
    "input_boolean.hall_awaiting_sonya_tea_comment",
    "input_boolean.tg_awaiting_sonya_water_wants",
    "input_boolean.tg_awaiting_sonya_water_comment",
    "input_boolean.sonya_direct_awaiting_water_comment",
)


@dataclass
class AdminModeSession:
    mode: AdminMode
    room: AdminRoom | None = None
    room_label: str | None = None
    entity_id: str | None = None
    selected_volume: float | None = None
    previous_volume: float | None = None
    pending_dialog: str | None = None
    pending_stop_after_answer: bool = False
    active: bool = False


class AdminModeManager:
    def __init__(self) -> None:
        self._sessions: dict[int, AdminModeSession] = {}

    def start(self, user_id: int, mode: AdminMode) -> AdminModeSession:
        session = AdminModeSession(mode=mode)
        self._sessions[user_id] = session
        return session

    def get(self, user_id: int) -> AdminModeSession | None:
        return self._sessions.get(user_id)

    def set_room(self, user_id: int, room: AdminRoom) -> AdminModeSession | None:
        session = self.get(user_id)
        if session is None:
            return None
        room_label, entity_id = ADMIN_ROOM_OPTIONS[room]
        session.room = room
        session.room_label = room_label
        session.entity_id = entity_id
        return session

    def activate(self, user_id: int, *, volume: float, previous_volume: float | None) -> AdminModeSession | None:
        session = self.get(user_id)
        if session is None:
            return None
        session.selected_volume = volume
        session.previous_volume = previous_volume
        session.active = True
        return session

    def set_pending_dialog(self, user_id: int, dialog: str | None) -> None:
        session = self.get(user_id)
        if session is not None:
            session.pending_dialog = dialog

    def set_pending_stop_after_answer(self, user_id: int, enabled: bool) -> None:
        session = self.get(user_id)
        if session is not None:
            session.pending_stop_after_answer = enabled

    def find_by_pending_dialog(self, dialog: str) -> tuple[int, AdminModeSession] | None:
        for user_id, session in self._sessions.items():
            if session.active and session.mode == "talk" and session.pending_dialog == dialog:
                return user_id, session
        return None

    def clear(self, user_id: int) -> AdminModeSession | None:
        return self._sessions.pop(user_id, None)
