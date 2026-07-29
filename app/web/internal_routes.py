from __future__ import annotations

import hmac
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from app.config import Settings
from app.keyboards.coffee import coffee_turn_off_only
from app.keyboards.main import main_menu
from app.messages import coffee as coffee_messages
from app.messages.common import admin_menu_text
from app.services.admin_modes import ADMIN_TALK_DIALOGS, AdminModeManager, AdminModeSession
from app.services.app_state import AppStateStore
from app.services.coffee_alerts import CoffeeAlertScheduler
from app.services.coffee_timing_policy import CoffeeTimingPolicyService, TimingPolicyError
from app.services.coffee_machine import set_coffee_machine, turn_on_coffee_machine
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.workflows.coffee import CoffeeWorkflow, SonyaAnswer
from app.workflows.reminders import ReminderWorkflow
from app.workflows.tea import TeaAnswer, TeaWorkflow
from app.workflows.water import WaterAnswer, WaterWorkflow

LOGGER = logging.getLogger(__name__)


async def health_live(_: web.Request) -> web.Response:
    return web.json_response(
        {"status": "live", "observed_at": datetime.now(timezone.utc).isoformat()}
    )


async def health_ready(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    ha: HomeAssistantClient = request.app["ha"]
    timing_policy: CoffeeTimingPolicyService = request.app["coffee_timing_policy"]
    bot = request.app["bot"]
    try:
        coffee_state = await ha.get_state(settings.coffee_switch_entity)
        ha_ready = coffee_state is not None
        timing_ready = timing_policy.status == "ready"
    except (HomeAssistantError, TimingPolicyError):
        ha_ready = False
        timing_ready = False
    telegram_ready = _telegram_transport_ready(bot)
    ready = ha_ready and telegram_ready
    return web.json_response(
        {
            "status": "ready" if ready else "not_ready",
            "telegram": "ready" if telegram_ready else "not_ready",
            "home_assistant": "ready" if ha_ready else "not_ready",
            "timing_helpers": "ready" if timing_ready else "not_ready",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
        status=200 if ready else 503,
    )


async def health_details(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    settings: Settings = request.app["settings"]
    timing_policy: CoffeeTimingPolicyService = request.app["coffee_timing_policy"]
    policy = timing_policy.policy
    bot = request.app["bot"]
    timing_status = timing_policy.status
    return web.json_response(
        {
            "status": "running",
            "telegram_transport": "ready" if _telegram_transport_ready(bot) else "not_ready",
            "home_assistant": "ready" if timing_status == "ready" else "unknown",
            "timing_helpers": timing_status,
            "timing_policy_fetched_at": policy.fetched_at if policy else None,
            "version": settings.app_version,
            "commit": settings.app_commit,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    )


async def health(request: web.Request) -> web.Response:
    return await health_live(request)


def _telegram_transport_ready(bot: object) -> bool:
    session = getattr(bot, "session", None)
    if session is None:
        return False
    closed = getattr(session, "closed", None)
    if isinstance(closed, bool):
        return not closed
    aiohttp_session = getattr(session, "_session", None)
    return aiohttp_session is None or not bool(getattr(aiohttp_session, "closed", False))


def _check_internal_secret(request: web.Request) -> None:
    settings: Settings = request.app["settings"]
    if request.headers.get("X-Internal-Secret") != settings.internal_webhook_secret:
        raise web.HTTPUnauthorized(text="Invalid internal secret")


def _shortcuts_json_error(error: str, message: str, *, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": error, "message": message}, status=status)


def _check_shortcuts_auth(request: web.Request) -> web.Response | None:
    settings: Settings = request.app["settings"]
    LOGGER.info("Shortcut espresso authorization check started")
    if not settings.shortcuts_secret_token:
        LOGGER.warning("Shortcut espresso endpoint is disabled: SHORTCUTS_SECRET_TOKEN is not configured")
        return _shortcuts_json_error("unauthorized", "Команда отклонена: неверный токен", status=503)

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        LOGGER.warning("Shortcut espresso authorization failed: missing or malformed Authorization header")
        return _shortcuts_json_error("unauthorized", "Команда отклонена: неверный токен", status=401)

    provided_token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided_token, settings.shortcuts_secret_token):
        LOGGER.warning("Shortcut espresso authorization failed: invalid bearer token")
        return _shortcuts_json_error("unauthorized", "Команда отклонена: неверный токен", status=403)

    LOGGER.info("Shortcut espresso authorization succeeded")
    return None


async def _parse_sonya_answer(request: web.Request) -> SonyaAnswer:
    payload = await request.json()
    answer = SonyaAnswer(
        request_id=payload.get("request_id"),
        answer=str(payload.get("answer", "")),
        intent=str(payload.get("intent", "")),
        dialog=str(payload.get("dialog", "")),
        source=payload.get("source"),
        comment=payload.get("comment"),
    )
    LOGGER.info(
        "Internal coffee event: path=%s dialog=%s intent=%s answer=%r",
        request.path,
        answer.dialog,
        answer.intent,
        answer.answer,
    )
    return answer


async def _parse_tea_answer(request: web.Request) -> TeaAnswer:
    payload = await request.json()
    answer = TeaAnswer(
        request_id=payload.get("request_id"),
        answer=str(payload.get("answer", "")),
        intent=str(payload.get("intent", "")),
        dialog=str(payload.get("dialog", "")),
        source=payload.get("source"),
        comment=payload.get("comment"),
    )
    LOGGER.info(
        "Internal tea event: path=%s dialog=%s intent=%s answer=%r",
        request.path,
        answer.dialog,
        answer.intent,
        answer.answer,
    )
    return answer


async def _parse_water_answer(request: web.Request) -> WaterAnswer:
    payload = await request.json()
    answer = WaterAnswer(
        request_id=payload.get("request_id"),
        answer=str(payload.get("answer", "")),
        intent=str(payload.get("intent", "")),
        dialog=str(payload.get("dialog", "")),
        source=payload.get("source"),
    )
    LOGGER.info(
        "Internal water event: path=%s dialog=%s intent=%s answer=%r",
        request.path,
        answer.dialog,
        answer.intent,
        answer.answer,
    )
    return answer


async def _parse_admin_talk_answer(request: web.Request) -> dict[str, str]:
    payload = await request.json()
    answer = {
        "answer": str(payload.get("answer", "")),
        "intent": str(payload.get("intent", "")),
        "dialog": str(payload.get("dialog", "")),
    }
    LOGGER.info(
        "Internal admin talk event: path=%s dialog=%s intent=%s answer=%r",
        request.path,
        answer["dialog"],
        answer["intent"],
        answer["answer"],
    )
    return answer


async def sonya_wants_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        await workflow.handle_wants_answer(await _parse_sonya_answer(request))
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def sonya_type_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        await workflow.handle_temperature_answer(await _parse_sonya_answer(request), direct=False)
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def sonya_direct_type_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        await workflow.handle_temperature_answer(await _parse_sonya_answer(request), direct=True)
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def sonya_temperature_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        answer = await _parse_sonya_answer(request)
        await workflow.handle_temperature_answer(answer, direct=answer.dialog == "sonya_direct_coffee_temperature")
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def sonya_syrup_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        answer = await _parse_sonya_answer(request)
        await workflow.handle_syrup_answer(answer, direct=answer.dialog == "sonya_direct_coffee_syrup")
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def sonya_coffee_comment_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        answer = await _parse_sonya_answer(request)
        await workflow.handle_comment_answer(answer, direct=answer.dialog == "sonya_direct_coffee_comment")
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def sonya_auto_enabled(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        await workflow.notify_auto_enabled(await _parse_sonya_answer(request))
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def sonya_hall_refused(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    try:
        await workflow.notify_hall_refused()
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def coffee_warmed_up_alert(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    settings: Settings = request.app["settings"]
    app_state: AppStateStore = request.app["app_state"]
    timing_policy: CoffeeTimingPolicyService = request.app["coffee_timing_policy"]
    ha: HomeAssistantClient = request.app["ha"]
    bot = request.app["bot"]

    if not app_state.coffee_warmed_up_alert_enabled:
        LOGGER.info("Coffee warm-up alert skipped because this alert is disabled")
        return web.json_response({"ok": True, "sent": False, "disabled": True})

    switch_state = await ha.get_state(settings.coffee_switch_entity)
    if not switch_state or switch_state.get("state") != "on":
        LOGGER.info("Coffee warm-up alert skipped because coffee machine is not on")
        return web.json_response({"ok": True, "sent": False})
    runtime_seconds = _coffee_runtime_seconds(switch_state)
    delay_seconds = timing_policy.warmup_duration_seconds
    if delay_seconds is None:
        return web.json_response(
            {"ok": False, "sent": False, "error": "timing_policy_unavailable"},
            status=503,
        )
    if runtime_seconds < delay_seconds:
        LOGGER.info("Coffee warm-up legacy alert skipped because configured delay is not reached")
        return web.json_response({"ok": True, "sent": False, "not_due": True})
    if app_state.coffee_machine_state != "on":
        await app_state.mark_coffee_machine_on(str(switch_state.get("last_changed") or datetime.now(timezone.utc).isoformat()))
    if app_state.coffee_warmed_up_alert_sent or app_state.coffee_warmed_up_alert_telegram_sent:
        LOGGER.info("Coffee warm-up legacy alert skipped because it was already sent or inactive")
        return web.json_response({"ok": True, "sent": False, "already_sent": True})

    try:
        message = await bot.send_message(
            settings.telegram_admin_chat_id,
            coffee_messages.coffee_warmed_up(),
            reply_markup=coffee_turn_off_only(),
        )
    except Exception:
        LOGGER.exception("Coffee warm-up legacy alert send failed")
        return web.json_response({"ok": False, "sent": False, "error": "telegram_send_failed"}, status=502)
    await app_state.mark_coffee_warmed_up_alert_telegram_sent()
    LOGGER.info("Coffee warm-up legacy alert sent: message_id=%s", message.message_id)
    return web.json_response({"ok": True, "sent": True})


async def coffee_long_running_alert(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    settings: Settings = request.app["settings"]
    app_state: AppStateStore = request.app["app_state"]
    timing_policy: CoffeeTimingPolicyService = request.app["coffee_timing_policy"]
    ha: HomeAssistantClient = request.app["ha"]
    bot = request.app["bot"]

    if not app_state.coffee_long_running_alert_enabled:
        LOGGER.info("Coffee long-running alert skipped because this alert is disabled")
        return web.json_response({"ok": True, "sent": False, "disabled": True})

    switch_state = await ha.get_state(settings.coffee_switch_entity)
    if not switch_state or switch_state.get("state") != "on":
        LOGGER.info("Coffee long-running alert skipped because coffee machine is not on")
        return web.json_response({"ok": True, "sent": False})
    runtime_seconds = _coffee_runtime_seconds(switch_state)
    delay_seconds = timing_policy.long_running_threshold_seconds
    if delay_seconds is None:
        return web.json_response(
            {"ok": False, "sent": False, "error": "timing_policy_unavailable"},
            status=503,
        )
    if runtime_seconds < delay_seconds:
        LOGGER.info("Coffee long-running legacy alert skipped because configured delay is not reached")
        return web.json_response({"ok": True, "sent": False, "not_due": True})
    if app_state.coffee_machine_state != "on":
        await app_state.mark_coffee_machine_on(str(switch_state.get("last_changed") or datetime.now(timezone.utc).isoformat()))
    if app_state.coffee_long_running_alert_sent or app_state.coffee_long_running_alert_telegram_sent:
        LOGGER.info("Coffee long-running legacy alert skipped because it was already sent or inactive")
        return web.json_response({"ok": True, "sent": False, "already_sent": True})

    runtime_text = _coffee_runtime_text(switch_state)
    try:
        message = await bot.send_message(
            settings.telegram_admin_chat_id,
            coffee_messages.coffee_warning_long_running_text(runtime_text),
            reply_markup=coffee_turn_off_only(),
        )
    except Exception:
        LOGGER.exception("Coffee long-running legacy alert send failed")
        return web.json_response({"ok": False, "sent": False, "error": "telegram_send_failed"}, status=502)
    await app_state.mark_coffee_long_running_alert_telegram_sent()
    LOGGER.info("Coffee long-running legacy alert sent: message_id=%s", message.message_id)
    return web.json_response({"ok": True, "sent": True})


async def coffee_machine_state(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON")

    state = str(payload.get("state") or "").strip().lower()
    if state not in {"on", "off"}:
        raise web.HTTPBadRequest(text="Unsupported state")

    scheduler: CoffeeAlertScheduler = request.app["coffee_alert_scheduler"]
    await scheduler.handle_state(state, changed_at=str(payload.get("changed_at") or "") or None)
    LOGGER.info(
        "Coffee machine state endpoint handled: entity_id=%s state=%s changed_at=%s",
        payload.get("entity_id"),
        state,
        payload.get("changed_at"),
    )
    return web.json_response({"ok": True, "state": state})


async def admin_talk_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    settings: Settings = request.app["settings"]
    admin_modes: AdminModeManager = request.app["admin_modes"]
    ha: HomeAssistantClient = request.app["ha"]
    bot = request.app["bot"]

    answer = await _parse_admin_talk_answer(request)
    dialog = answer["dialog"]
    if dialog not in set(ADMIN_TALK_DIALOGS.values()):
        raise web.HTTPBadRequest(text="Unsupported dialog")

    matched = admin_modes.find_by_pending_dialog(dialog)
    if matched is None:
        LOGGER.info("Admin talk answer ignored without pending session: dialog=%s answer=%r", dialog, answer["answer"])
        return web.json_response({"ok": True, "sent": False})

    user_id, session = matched
    admin_modes.set_pending_dialog(user_id, None)
    await bot.send_message(settings.telegram_admin_chat_id, f"Соня сказала: {answer['answer']}")
    LOGGER.info("Admin talk answer sent to Telegram: dialog=%s user_id=%s", dialog, user_id)
    if session.pending_stop_after_answer:
        stopped_session = admin_modes.clear(user_id)
        if stopped_session is not None:
            await _restore_admin_mode_volume(stopped_session, ha)
        await bot.send_message(settings.telegram_admin_chat_id, admin_menu_text(), reply_markup=main_menu())
        LOGGER.info("Admin talk mode stopped after answer: dialog=%s user_id=%s", dialog, user_id)
    return web.json_response({"ok": True, "sent": True})


async def _restore_admin_mode_volume(session: AdminModeSession, ha: HomeAssistantClient) -> None:
    if not session.entity_id or session.previous_volume is None:
        LOGGER.info("Admin mode volume restore skipped: entity_id=%s previous_volume=%s", session.entity_id, session.previous_volume)
        return
    try:
        await ha.set_volume(session.entity_id, session.previous_volume)
    except HomeAssistantError:
        LOGGER.exception("Cannot restore admin mode volume: entity_id=%s previous_volume=%s", session.entity_id, session.previous_volume)
        return
    LOGGER.info("Admin mode volume restored: entity_id=%s previous_volume=%s", session.entity_id, session.previous_volume)


async def tea_wants_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        answer = await _parse_tea_answer(request)
        flow_type = "hall" if answer.dialog == "hall_ask_sonya_wants_tea" else "telegram"
        await workflow.handle_wants_answer(answer, flow_type=flow_type)
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def tea_keep_warm_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        answer = await _parse_tea_answer(request)
        if answer.dialog == "sonya_direct_tea_keep_warm":
            flow_type = "direct"
        elif answer.dialog == "hall_ask_sonya_tea_keep_warm":
            flow_type = "hall"
        else:
            flow_type = "telegram"
        await workflow.handle_keep_warm_answer(answer, flow_type=flow_type)
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def tea_comment_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        answer = await _parse_tea_answer(request)
        if answer.dialog == "sonya_direct_tea_comment":
            flow_type = "direct"
        elif answer.dialog == "hall_ask_sonya_tea_comment":
            flow_type = "hall"
        else:
            flow_type = "telegram"
        await workflow.handle_comment_answer(answer, flow_type=flow_type)
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def tea_direct_request(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        await workflow.start_direct_request()
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def tea_hall_request(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        await workflow.start_hall_question()
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def tea_auto_enabled(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        await workflow.notify_auto_enabled(await _parse_tea_answer(request))
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def tea_hall_refused(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        await workflow.notify_hall_refused()
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def water_wants_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: WaterWorkflow = request.app["water_workflow"]
    try:
        await workflow.handle_wants_answer(await _parse_water_answer(request))
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def water_comment_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: WaterWorkflow = request.app["water_workflow"]
    try:
        answer = await _parse_water_answer(request)
        await workflow.handle_comment_answer(answer, direct=answer.dialog == "sonya_direct_water_comment")
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def water_direct_request(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: WaterWorkflow = request.app["water_workflow"]
    try:
        await workflow.start_direct_request()
    except Exception:
        await workflow.reset_after_failure()
        raise
    return web.json_response({"ok": True})


async def alice_reminder_create(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    payload = await request.json()
    text = str(payload.get("text") or payload.get("answer") or "")
    LOGGER.info(
        "Internal reminder create request received: source=alice dialog=%s intent=%s text_len=%s",
        payload.get("dialog"),
        payload.get("intent"),
        len(text),
    )
    workflow: ReminderWorkflow = request.app["reminder_workflow"]
    result = await workflow.create_from_text(text, source="alice")
    if result is None:
        return web.json_response(
            {
                "ok": False,
                "error": "parse_error",
                "message": "Не поняла, через сколько напомнить",
            },
            status=400,
        )
    reminder, parsed = result
    reminder_settings = await workflow.get_settings()
    if reminder_settings.voice_enabled:
        LOGGER.info(
            "Reminder voice announcement will be sent by Home Assistant: voice_enabled=true voice_station_entity_id=%s",
            reminder_settings.voice_station_entity_id,
        )
    else:
        LOGGER.info("Reminder voice announcement skipped because voice_enabled=false")
    return web.json_response(
        {
            "ok": True,
            "message": f"Поняла, отправлю напоминание через {parsed.human_delay_text}",
            "reminder_id": reminder.id,
            "delay_text": parsed.human_delay_text,
            "voice_enabled": reminder_settings.voice_enabled,
            "voice_station_entity_id": reminder_settings.voice_station_entity_id,
        }
    )


async def shortcut_coffee_gif(_: web.Request) -> web.FileResponse:
    asset_path = Path(__file__).resolve().parents[2] / "notification-assets" / "coffee.gif"
    if not asset_path.is_file():
        LOGGER.warning("Coffee warmup GIF asset not found: path=%s", asset_path)
        raise web.HTTPNotFound(text="coffee.gif not found")
    return web.FileResponse(
        asset_path,
        headers={
            "Content-Type": "image/gif",
            "Cache-Control": "public, max-age=86400",
        },
    )


async def _send_shortcut_status_notification(settings: Settings, ha: HomeAssistantClient, message: str) -> None:
    if not settings.ha_mobile_notify_services:
        LOGGER.warning("Shortcut coffee status push skipped because HA mobile notify services are not configured")
        return
    tag = "coffee_machine_shortcut_status"
    for service in settings.ha_mobile_notify_services:
        try:
            await ha.notify(
                service,
                title="\u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430",
                message=message,
                data={"tag": tag},
            )
        except HomeAssistantError as exc:
            LOGGER.exception("Shortcut coffee status push failed: service=%s reason=%r", service, exc)
        else:
            LOGGER.info("Shortcut coffee status push sent: service=%s", service)

    async def clear_later() -> None:
        await asyncio.sleep(4)
        for service in settings.ha_mobile_notify_services:
            try:
                await ha.notify(
                    service,
                    title="\u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430",
                    message="clear_notification",
                    data={"tag": tag},
                )
            except HomeAssistantError as exc:
                LOGGER.exception("Shortcut coffee status push cleanup failed: service=%s reason=%r", service, exc)

    asyncio.create_task(clear_later())


async def shortcut_espresso(request: web.Request) -> web.Response:
    LOGGER.info("Shortcut espresso request received")
    auth_error = _check_shortcuts_auth(request)
    if auth_error is not None:
        return auth_error

    try:
        payload = await request.json()
    except Exception:
        LOGGER.warning("Shortcut espresso request rejected: invalid JSON body")
        return _shortcuts_json_error("invalid_action", "Неизвестная команда", status=400)

    action = str(payload.get("action", "")).strip()
    LOGGER.info("Shortcut espresso action requested: action=%s", action)
    if action not in {"turn_on", "turn_off"}:
        LOGGER.warning("Shortcut espresso request rejected: unsupported action=%s", action)
        return _shortcuts_json_error("invalid_action", "Неизвестная команда", status=400)

    settings: Settings = request.app["settings"]
    ha: HomeAssistantClient = request.app["ha"]
    scheduler: CoffeeAlertScheduler = request.app["coffee_alert_scheduler"]
    try:
        if action == "turn_on":
            result = await turn_on_coffee_machine(ha, settings, source="shortcut:/shortcut/espresso")
            if result.already_on:
                runtime_text = result.runtime_text or "00:00"
                message = (
                    "\u2615 \u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430 \u0443\u0436\u0435 "
                    f"\u0432\u043a\u043b\u044e\u0447\u0435\u043d\u0430. \u0412\u0440\u0435\u043c\u044f \u0440\u0430\u0431\u043e\u0442\u044b: {runtime_text}"
                )
                await _send_shortcut_status_notification(settings, ha, message)
                return web.json_response(
                    {
                        "ok": True,
                        "action": action,
                        "status": "already_on",
                        "message": (
                            "\u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430 \u0443\u0436\u0435 "
                            f"\u0432\u043a\u043b\u044e\u0447\u0435\u043d\u0430. \u0412\u0440\u0435\u043c\u044f \u0440\u0430\u0431\u043e\u0442\u044b: {runtime_text}"
                        ),
                    }
                )
            await scheduler.handle_state("on")
            await _send_shortcut_status_notification(
                settings,
                ha,
                "\u2615 \u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430 \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u0430",
            )
        else:
            await set_coffee_machine(ha, settings, action, source="shortcut:/shortcut/espresso")  # type: ignore[arg-type]
            await scheduler.handle_state("off")
    except HomeAssistantError:
        LOGGER.exception("Shortcut espresso coffee action failed: action=%s", action)
        return _shortcuts_json_error(
            "home_assistant_error",
            "Не удалось выполнить команду для кофемашины",
            status=502,
        )

    return web.json_response(
        {
            "ok": True,
            "action": action,
            "message": "Кофемашина включена" if action == "turn_on" else "Кофемашина выключена",
        }
    )


def setup_internal_routes(app: web.Application) -> None:
    app.router.add_get("/health", health)
    app.router.add_get("/health/live", health_live)
    app.router.add_get("/health/ready", health_ready)
    app.router.add_get("/health/details", health_details)
    app.router.add_post("/shortcut/espresso", shortcut_espresso)
    app.router.add_get("/shortcut/assets/coffee.gif", shortcut_coffee_gif)
    app.router.add_post("/internal/coffee/sonya-wants-answer", sonya_wants_answer)
    app.router.add_post("/internal/coffee/sonya-type-answer", sonya_type_answer)
    app.router.add_post("/internal/coffee/sonya-direct-type-answer", sonya_direct_type_answer)
    app.router.add_post("/internal/coffee/sonya-temperature-answer", sonya_temperature_answer)
    app.router.add_post("/internal/coffee/sonya-syrup-answer", sonya_syrup_answer)
    app.router.add_post("/internal/coffee/sonya-comment-answer", sonya_coffee_comment_answer)
    app.router.add_post("/internal/coffee/sonya-auto-enabled", sonya_auto_enabled)
    app.router.add_post("/internal/coffee/sonya-hall-refused", sonya_hall_refused)
    app.router.add_post("/internal/coffee-machine/state", coffee_machine_state)
    app.router.add_post("/internal/coffee/warmed-up-alert", coffee_warmed_up_alert)
    app.router.add_post("/internal/coffee/long-running-alert", coffee_long_running_alert)
    app.router.add_post("/internal/admin/talk-answer", admin_talk_answer)
    app.router.add_post("/internal/tea/sonya-wants-answer", tea_wants_answer)
    app.router.add_post("/internal/tea/sonya-keep-warm-answer", tea_keep_warm_answer)
    app.router.add_post("/internal/tea/sonya-comment-answer", tea_comment_answer)
    app.router.add_post("/internal/tea/sonya-direct-request", tea_direct_request)
    app.router.add_post("/internal/tea/hall-request", tea_hall_request)
    app.router.add_post("/internal/tea/sonya-auto-enabled", tea_auto_enabled)
    app.router.add_post("/internal/tea/sonya-hall-refused", tea_hall_refused)
    app.router.add_post("/internal/water/sonya-wants-answer", water_wants_answer)
    app.router.add_post("/internal/water/sonya-comment-answer", water_comment_answer)
    app.router.add_post("/internal/water/sonya-direct-request", water_direct_request)
    app.router.add_post("/internal/reminders/alice-create", alice_reminder_create)


def _coffee_runtime_text(switch_state: dict) -> str:
    started_at = _parse_ha_datetime(str(switch_state.get("last_changed", "")))
    if started_at is None:
        return "неизвестно"

    total_minutes = _coffee_runtime_seconds(switch_state) // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours}ч {minutes}мин"
    return f"{minutes}мин"


def _parse_ha_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        LOGGER.warning("Cannot parse Home Assistant datetime: %s", value)
        return None


def _coffee_runtime_seconds(switch_state: dict) -> int:
    started_at = _parse_ha_datetime(str(switch_state.get("last_changed", "")))
    if started_at is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
