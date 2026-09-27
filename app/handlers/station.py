"""Admin-only Telegram controls for the fixed Station target and saved presets."""
from html import escape
from time import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.config import Settings
from app.keyboards.main import inline_button, station_menu
from app.services.app_state import AppStatePersistenceError, AppStateRevisionConflict, AppStateStore, _station_text
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.station_actions import STATION_COMMANDS, StationActionService

router = Router()


class PresetDraft(StatesGroup):
    title = State()
    command = State()
    confirm = State()


async def _draft_expired(state: FSMContext) -> bool:
    data = await state.get_data()
    if not isinstance(data.get("expires_at"), (int, float)) or time() > data["expires_at"]:
        await state.clear()
        return True
    return False


def _music_keyboard(presets: list[dict], *, manage: bool = False) -> InlineKeyboardMarkup:
    rows = [[inline_button(text=("🗑 " if manage else "") + item["title"],
                           callback_data=f"station:preset:{'delete' if manage else 'play'}:{item['id']}")]
            for item in presets]
    if manage:
        rows.append([inline_button(text="➕ Добавить", callback_data="station:preset:add")])
        rows.append([inline_button(text="← Назад", callback_data="station:music")])
    else:
        rows.append([inline_button(text="⚙ Подборки", callback_data="station:preset:manage")])
        rows.append([inline_button(text="← Назад", callback_data="station:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_presets(callback: CallbackQuery, app_state: AppStateStore, *, manage: bool = False,
                        notice: str | None = None) -> None:
    try:
        snapshot = await app_state.station_presets()
    except AppStatePersistenceError:
        await callback.answer("Список временно недоступен", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Подборки" if manage else "Что включить?",
                                         reply_markup=_music_keyboard(snapshot["presets"], manage=manage))
    if notice:
        await callback.answer(notice, show_alert=True)
    else:
        await callback.answer()


@router.callback_query(F.data == "station:music")
async def station_music(callback: CallbackQuery, settings: Settings, app_state: AppStateStore) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _show_presets(callback, app_state)


@router.callback_query(F.data == "station:preset:manage")
async def station_preset_manage(callback: CallbackQuery, settings: Settings, app_state: AppStateStore) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await _show_presets(callback, app_state, manage=True)


@router.callback_query(F.data.startswith("station:preset:play:"))
async def station_preset_play(callback: CallbackQuery, settings: Settings, ha: HomeAssistantClient,
                              app_state: AppStateStore) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        outcome = await StationActionService(ha, settings).dispatch_preset(
            (callback.data or "").removeprefix("station:preset:play:"), app_state)
    except ValueError:
        await callback.answer("Подборка не найдена", show_alert=True)
        return
    except (HomeAssistantError, AppStatePersistenceError):
        await callback.answer("Не удалось отправить команду", show_alert=True)
        return
    await callback.answer("Команда отправлена" if outcome == "dispatched" else "Результат отправки неизвестен",
                          show_alert=outcome != "dispatched")


@router.callback_query(F.data == "station:preset:add")
async def station_preset_add(callback: CallbackQuery, settings: Settings, state: FSMContext,
                             app_state: AppStateStore) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    try:
        snapshot = await app_state.station_presets()
    except AppStatePersistenceError:
        await callback.answer("Список временно недоступен", show_alert=True)
        return
    await state.update_data(revision=snapshot["revision"], expires_at=time() + 600)
    await state.set_state(PresetDraft.title)
    if callback.message:
        await callback.message.edit_text("Как назвать кнопку?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [inline_button(text="Отмена", callback_data="station:preset:cancel")]]))
    await callback.answer()


@router.message(PresetDraft.title)
async def station_preset_title(message: Message, settings: Settings, state: FSMContext) -> None:
    if not settings.is_admin_user(message.from_user.id):
        await state.clear()
        return
    if await _draft_expired(state):
        await message.answer("Время добавления истекло. Начните заново.")
        return
    try:
        title = _station_text(message.text, 32)
    except ValueError:
        await message.answer("Название должно содержать от 1 до 32 символов. Попробуйте ещё раз.")
        return
    await state.update_data(title=title)
    await state.set_state(PresetDraft.command)
    await message.answer("Какую команду отправлять Алисе?")


@router.message(PresetDraft.command)
async def station_preset_command(message: Message, settings: Settings, state: FSMContext) -> None:
    if not settings.is_admin_user(message.from_user.id):
        await state.clear()
        return
    if await _draft_expired(state):
        await message.answer("Время добавления истекло. Начните заново.")
        return
    try:
        command = _station_text(message.text, 160)
    except ValueError:
        await message.answer("Команда должна содержать от 1 до 160 символов. Попробуйте ещё раз.")
        return
    await state.update_data(command=command)
    await state.set_state(PresetDraft.confirm)
    data = await state.get_data()
    await message.answer(f"Название: {escape(data['title'])}\nКоманда: {escape(command)}", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [inline_button(text="✅ Сохранить", callback_data="station:preset:save")],
                             [inline_button(text="Отмена", callback_data="station:preset:cancel")]]))


@router.message(PresetDraft.confirm)
async def station_preset_unexpected(message: Message) -> None:
    await message.answer("Выберите «Сохранить» или «Отмена» на сообщении выше.")


@router.callback_query(F.data == "station:preset:cancel")
async def station_preset_cancel(callback: CallbackQuery, settings: Settings, state: FSMContext,
                                app_state: AppStateStore) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await _show_presets(callback, app_state, manage=True)


@router.callback_query(F.data == "station:preset:save")
async def station_preset_save(callback: CallbackQuery, settings: Settings, state: FSMContext,
                              app_state: AppStateStore) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if await state.get_state() != PresetDraft.confirm.state:
        await callback.answer("Добавление уже завершено", show_alert=True)
        return
    if await _draft_expired(state):
        await callback.answer("Время добавления истекло. Начните заново.", show_alert=True)
        return
    data = await state.get_data()
    try:
        await app_state.add_station_preset(expected_revision=data["revision"],
                                           title=data["title"], command=data["command"])
    except AppStateRevisionConflict:
        await state.clear()
        await callback.answer("Список изменился. Откройте актуальную версию.", show_alert=True)
        return
    except (ValueError, AppStatePersistenceError, KeyError):
        await callback.answer("Не удалось сохранить подборку", show_alert=True)
        return
    await state.clear()
    await _show_presets(callback, app_state, manage=True)


@router.callback_query(F.data.startswith("station:preset:delete:"))
async def station_preset_delete_prompt(callback: CallbackQuery, settings: Settings,
                                       app_state: AppStateStore, state: FSMContext) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        snapshot = await app_state.station_presets()
    except AppStatePersistenceError:
        await callback.answer("Список временно недоступен", show_alert=True)
        return
    preset_id = (callback.data or "").removeprefix("station:preset:delete:")
    item = next((item for item in snapshot["presets"] if item["id"] == preset_id), None)
    if item is None:
        await callback.answer("Подборка не найдена", show_alert=True)
        return
    await state.update_data(delete_id=preset_id, delete_revision=snapshot["revision"])
    if callback.message:
        await callback.message.edit_text(f"Удалить «{escape(item['title'])}»?", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [inline_button(text="🗑 Удалить", callback_data="station:preset:delete_confirm")],
                [inline_button(text="Отмена", callback_data="station:preset:cancel")]]))
    await callback.answer()


@router.callback_query(F.data == "station:preset:delete_confirm")
async def station_preset_delete_confirm(callback: CallbackQuery, settings: Settings,
                                        app_state: AppStateStore, state: FSMContext) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    if "delete_id" not in data:
        await callback.answer("Выберите подборку заново", show_alert=True)
        return
    notice = None
    try:
        await app_state.delete_station_preset(expected_revision=data["delete_revision"],
                                               preset_id=data["delete_id"])
    except AppStateRevisionConflict:
        notice = "Список изменился. Показана актуальная версия."
    except (KeyError, AppStatePersistenceError):
        notice = "Не удалось удалить подборку"
    await state.clear()
    await _show_presets(callback, app_state, manage=True, notice=notice)


@router.callback_query(F.data == "station:menu")
async def show_station_menu(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Станция Mini 2", reply_markup=station_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("station:"))
async def station_action(callback: CallbackQuery, settings: Settings, ha: HomeAssistantClient) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = (callback.data or "").removeprefix("station:")
    if action not in STATION_COMMANDS:
        await callback.answer("Неизвестная команда", show_alert=True)
        return
    try:
        outcome = await StationActionService(ha, settings).dispatch(action)
    except HomeAssistantError:
        await callback.answer("Не удалось отправить команду", show_alert=True)
        return
    await callback.answer(
        "Команда отправлена" if outcome == "dispatched" else "Результат отправки неизвестен",
        show_alert=outcome != "dispatched",
    )
