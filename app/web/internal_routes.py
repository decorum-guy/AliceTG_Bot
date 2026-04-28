from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiohttp import web

from app.config import Settings
from app.keyboards.coffee import coffee_turn_off_only
from app.services.home_assistant import HomeAssistantClient
from app.workflows.coffee import CoffeeWorkflow, SonyaAnswer
from app.workflows.tea import TeaAnswer, TeaWorkflow

LOGGER = logging.getLogger(__name__)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def _check_internal_secret(request: web.Request) -> None:
    settings: Settings = request.app["settings"]
    if request.headers.get("X-Internal-Secret") != settings.internal_webhook_secret:
        raise web.HTTPUnauthorized(text="Invalid internal secret")


async def _parse_sonya_answer(request: web.Request) -> SonyaAnswer:
    payload = await request.json()
    answer = SonyaAnswer(
        request_id=payload.get("request_id"),
        answer=str(payload.get("answer", "")),
        intent=str(payload.get("intent", "")),
        dialog=str(payload.get("dialog", "")),
        source=payload.get("source"),
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
    )
    LOGGER.info(
        "Internal tea event: path=%s dialog=%s intent=%s answer=%r",
        request.path,
        answer.dialog,
        answer.intent,
        answer.answer,
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
    ha: HomeAssistantClient = request.app["ha"]
    bot = request.app["bot"]

    switch_state = await ha.get_state(settings.coffee_switch_entity)
    if not switch_state or switch_state.get("state") != "on":
        LOGGER.info("Coffee warm-up alert skipped because coffee machine is not on")
        return web.json_response({"ok": True, "sent": False})

    await bot.send_message(
        settings.telegram_admin_chat_id,
        "Уведомление: Кофемашина разогрета.",
        reply_markup=coffee_turn_off_only(),
    )
    return web.json_response({"ok": True, "sent": True})


async def coffee_long_running_alert(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    settings: Settings = request.app["settings"]
    ha: HomeAssistantClient = request.app["ha"]
    bot = request.app["bot"]

    switch_state = await ha.get_state(settings.coffee_switch_entity)
    if not switch_state or switch_state.get("state") != "on":
        LOGGER.info("Coffee long-running alert skipped because coffee machine is not on")
        return web.json_response({"ok": True, "sent": False})

    runtime_text = _coffee_runtime_text(switch_state)
    await bot.send_message(
        settings.telegram_admin_chat_id,
        "Предупреждение: Внимание, кофемашина работает непрерывно уже 1 час, рекомендуется выключить.\n"
        f"Время непрерывной работы: {runtime_text}",
        reply_markup=coffee_turn_off_only(),
    )
    return web.json_response({"ok": True, "sent": True})


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


async def tea_keep_warm_temperature_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: TeaWorkflow = request.app["tea_workflow"]
    try:
        answer = await _parse_tea_answer(request)
        if answer.dialog == "sonya_direct_tea_keep_warm_temperature":
            flow_type = "direct"
        elif answer.dialog == "hall_ask_sonya_tea_keep_warm_temperature":
            flow_type = "hall"
        else:
            flow_type = "telegram"
        await workflow.handle_keep_warm_temperature_answer(answer, flow_type=flow_type)
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


def setup_internal_routes(app: web.Application) -> None:
    app.router.add_get("/health", health)
    app.router.add_post("/internal/coffee/sonya-wants-answer", sonya_wants_answer)
    app.router.add_post("/internal/coffee/sonya-type-answer", sonya_type_answer)
    app.router.add_post("/internal/coffee/sonya-direct-type-answer", sonya_direct_type_answer)
    app.router.add_post("/internal/coffee/sonya-temperature-answer", sonya_temperature_answer)
    app.router.add_post("/internal/coffee/sonya-syrup-answer", sonya_syrup_answer)
    app.router.add_post("/internal/coffee/sonya-auto-enabled", sonya_auto_enabled)
    app.router.add_post("/internal/coffee/sonya-hall-refused", sonya_hall_refused)
    app.router.add_post("/internal/coffee/warmed-up-alert", coffee_warmed_up_alert)
    app.router.add_post("/internal/coffee/long-running-alert", coffee_long_running_alert)
    app.router.add_post("/internal/tea/sonya-wants-answer", tea_wants_answer)
    app.router.add_post("/internal/tea/sonya-keep-warm-answer", tea_keep_warm_answer)
    app.router.add_post("/internal/tea/sonya-keep-warm-temperature-answer", tea_keep_warm_temperature_answer)
    app.router.add_post("/internal/tea/sonya-direct-request", tea_direct_request)
    app.router.add_post("/internal/tea/hall-request", tea_hall_request)
    app.router.add_post("/internal/tea/sonya-auto-enabled", tea_auto_enabled)
    app.router.add_post("/internal/tea/sonya-hall-refused", tea_hall_refused)


def _coffee_runtime_text(switch_state: dict) -> str:
    started_at = _parse_ha_datetime(str(switch_state.get("last_changed", "")))
    if started_at is None:
        return "неизвестно"

    total_minutes = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds() // 60))
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
