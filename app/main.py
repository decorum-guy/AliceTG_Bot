from __future__ import annotations

import asyncio
import logging
import signal
import sys

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

LOGGER = logging.getLogger(__name__)


async def create_app() -> web.Application:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    if settings.telegram_proxy_url:
        LOGGER.info("Telegram API proxy is enabled")
    else:
        LOGGER.info("Telegram API proxy is disabled")
    LOGGER.info("Telegram mode: %s", settings.telegram_mode)

    session = AiohttpSession(proxy=settings.telegram_proxy_url) if settings.telegram_proxy_url else AiohttpSession()
    bot = Bot(token=settings.telegram_bot_token, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(start.router)
    dispatcher.include_router(coffee.router)
    dispatcher.include_router(common.router)

    ha = HomeAssistantClient(settings.ha_url, settings.ha_long_lived_token)
    storage = MemoryStorage()
    telegram_messages = TelegramMessages(bot)
    coffee_workflow = CoffeeWorkflow(settings, ha, storage, telegram_messages)

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

    if settings.telegram_mode == "webhook":
        setup_telegram_routes(app, settings.webhook_path)
    setup_internal_routes(app)

    async def close_resources(_: web.Application) -> None:
        await ha.close()
        await bot.session.close()

    app.on_cleanup.append(close_resources)
    return app


async def _run_polling(app: web.Application, stop_event: asyncio.Event) -> None:
    settings: Settings = app["settings"]
    bot: Bot = app["bot"]
    dispatcher: Dispatcher = app["dispatcher"]
    consecutive_errors = 0
    backoff_seconds = 2
    update_offset: int | None = None
    allowed_updates = dispatcher.resolve_used_update_types()

    if settings.telegram_drop_pending_updates:
        await bot.delete_webhook(drop_pending_updates=True)

    while not stop_event.is_set():
        try:
            updates = await asyncio.wait_for(
                bot.get_updates(
                    offset=update_offset,
                    timeout=settings.telegram_polling_timeout,
                    allowed_updates=allowed_updates,
                ),
                timeout=settings.telegram_polling_timeout + 15,
            )
            consecutive_errors = 0
            backoff_seconds = 2
            for update in updates:
                update_offset = update.update_id + 1
                await dispatcher.feed_update(bot, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_errors += 1
            LOGGER.exception(
                "Telegram polling failed: consecutive_errors=%s max_errors=%s",
                consecutive_errors,
                settings.telegram_polling_max_errors,
            )
            if consecutive_errors >= settings.telegram_polling_max_errors:
                LOGGER.error("Too many Telegram polling errors, exiting with code 1")
                sys.exit(1)
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 60)


async def _serve() -> None:
    app = await create_app()
    settings: Settings = app["settings"]
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.listen_host, port=settings.listen_port)
    await site.start()
    LOGGER.info("Aiohttp server started on %s:%s", settings.listen_host, settings.listen_port)

    polling_task: asyncio.Task[None] | None = None
    if settings.telegram_mode == "polling":
        polling_task = asyncio.create_task(_run_polling(app, stop_event))

    try:
        await stop_event.wait()
    finally:
        if polling_task:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        await runner.cleanup()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
