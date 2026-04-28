from __future__ import annotations

import logging

from aiohttp import web

from app.config import Settings
from app.workflows.coffee import CoffeeWorkflow, SonyaAnswer

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


async def sonya_wants_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    await workflow.handle_wants_answer(await _parse_sonya_answer(request))
    return web.json_response({"ok": True})


async def sonya_type_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    await workflow.handle_temperature_answer(await _parse_sonya_answer(request), direct=False)
    return web.json_response({"ok": True})


async def sonya_direct_type_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    await workflow.handle_temperature_answer(await _parse_sonya_answer(request), direct=True)
    return web.json_response({"ok": True})


async def sonya_temperature_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    answer = await _parse_sonya_answer(request)
    await workflow.handle_temperature_answer(answer, direct=answer.dialog == "sonya_direct_coffee_temperature")
    return web.json_response({"ok": True})


async def sonya_syrup_answer(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    answer = await _parse_sonya_answer(request)
    await workflow.handle_syrup_answer(answer, direct=answer.dialog == "sonya_direct_coffee_syrup")
    return web.json_response({"ok": True})


async def sonya_auto_enabled(request: web.Request) -> web.Response:
    _check_internal_secret(request)
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    await workflow.notify_auto_enabled(await _parse_sonya_answer(request))
    return web.json_response({"ok": True})


def setup_internal_routes(app: web.Application) -> None:
    app.router.add_get("/health", health)
    app.router.add_post("/internal/coffee/sonya-wants-answer", sonya_wants_answer)
    app.router.add_post("/internal/coffee/sonya-type-answer", sonya_type_answer)
    app.router.add_post("/internal/coffee/sonya-direct-type-answer", sonya_direct_type_answer)
    app.router.add_post("/internal/coffee/sonya-temperature-answer", sonya_temperature_answer)
    app.router.add_post("/internal/coffee/sonya-syrup-answer", sonya_syrup_answer)
    app.router.add_post("/internal/coffee/sonya-auto-enabled", sonya_auto_enabled)
