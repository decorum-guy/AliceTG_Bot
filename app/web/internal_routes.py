from __future__ import annotations

from aiohttp import web

from app.config import Settings
from app.workflows.coffee import CoffeeWorkflow, SonyaAnswer


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def sonya_answer(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    if request.headers.get("X-Internal-Secret") != settings.internal_webhook_secret:
        raise web.HTTPUnauthorized(text="Invalid internal secret")

    payload = await request.json()
    workflow: CoffeeWorkflow = request.app["coffee_workflow"]
    await workflow.handle_sonya_answer(
        SonyaAnswer(
            request_id=payload.get("request_id"),
            answer=str(payload.get("answer", "")),
            intent=str(payload.get("intent", "")),
            source=payload.get("source"),
        )
    )
    return web.json_response({"ok": True})


def setup_internal_routes(app: web.Application) -> None:
    app.router.add_get("/health", health)
    app.router.add_post("/internal/coffee/sonya-answer", sonya_answer)
