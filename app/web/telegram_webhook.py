from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web

from app.config import Settings

LOGGER = logging.getLogger(__name__)


async def telegram_webhook(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.telegram_webhook_secret:
        raise web.HTTPUnauthorized(text="Invalid Telegram secret token")

    bot: Bot = request.app["bot"]
    dispatcher: Dispatcher = request.app["dispatcher"]
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dispatcher.feed_update(bot, update)
    return web.json_response({"ok": True})


def setup_telegram_routes(app: web.Application, webhook_path: str) -> None:
    app.router.add_post(webhook_path, telegram_webhook)
