from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web

from app.config import Settings
from app.handlers import coffee, common, start
from app.services.home_assistant import HomeAssistantClient
from app.services.telegram_messages import TelegramMessages
from app.storage.memory import MemoryStorage
from app.web.internal_routes import setup_internal_routes
from app.web.telegram_webhook import setup_telegram_routes
from app.workflows.coffee import CoffeeWorkflow


async def create_app() -> web.Application:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    if settings.telegram_proxy_url:
        logging.getLogger(__name__).info("Telegram API proxy is enabled")
    else:
        logging.getLogger(__name__).info("Telegram API proxy is disabled")

    session = AiohttpSession(proxy=settings.telegram_proxy_url) if settings.telegram_proxy_url else AiohttpSession()
    bot = Bot(token=settings.telegram_bot_token, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(start.router)
    dispatcher.include_router(coffee.router)
    dispatcher.include_router(common.router)

    ha = HomeAssistantClient(settings.ha_url, settings.ha_long_lived_token)
    storage = MemoryStorage()
    telegram_messages = TelegramMessages(bot)
    coffee_workflow = CoffeeWorkflow(bot, settings, ha, storage)

    dispatcher["settings"] = settings
    dispatcher["ha"] = ha
    dispatcher["storage"] = storage
    dispatcher["telegram_messages"] = telegram_messages
    dispatcher["coffee_workflow"] = coffee_workflow

    app = web.Application()
    app["settings"] = settings
    app["bot"] = bot
    app["dispatcher"] = dispatcher
    app["ha"] = ha
    app["coffee_workflow"] = coffee_workflow

    setup_telegram_routes(app, settings.webhook_path)
    setup_internal_routes(app)

    async def close_resources(_: web.Application) -> None:
        await ha.close()
        await bot.session.close()

    app.on_cleanup.append(close_resources)
    return app


def main() -> None:
    web.run_app(create_app(), host="0.0.0.0", port=8088)


if __name__ == "__main__":
    main()
