from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web

from app.config import Settings
from app.handlers import admin_modes, coffee, common, planning, reminders, start, tea, water
from app.planning.legacy_import import build_reminder_store
from app.planning.backup import PlanningBackupError, PlanningBackupService
from app.planning.delivery import AliceSpokenDeliveryTransport, HomeAssistantMobileTransport, TelegramDeliveryTransport
from app.planning.db import PlanningDatabase, PlanningDatabaseConfig
from app.planning.health import PlanningHealthService
from app.planning.providers import (
    AiohttpCalDavTransport,
    ICloudCalDavProvider,
    ICloudCalendarRefreshLoop,
    ProviderCalendarCache,
    provider_stale_after_seconds,
)
from app.planning.scheduler import DurableReminderScheduler, validate_scheduler_modes
from app.planning.telegram_ui import PlanningTelegramService
from app.services.admin_modes import AdminModeManager
from app.services.app_state import AppStateStore
from app.services.coffee_alerts import CoffeeAlertScheduler
from app.services.coffee_timing_policy import (
    CoffeeTimingPolicyRefresher,
    CoffeeTimingPolicyService,
    TimingPolicyError,
)
from app.services.control_center_coffee import ControlCenterCoffeeActions
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.pushward import PushWardCoffeeActivity
from app.services.pushward_widgets import PushWardCoffeeWidget
from app.services.telegram_messages import TelegramMessages
from app.storage.memory import MemoryStorage
from app.web.internal_routes import setup_internal_routes
from app.web.telegram_webhook import setup_telegram_routes
from app.workflows.coffee import CoffeeWorkflow
from app.workflows.reminders import ReminderWorkflow
from app.workflows.tea import TeaWorkflow
from app.workflows.water import WaterWorkflow

LOGGER = logging.getLogger(__name__)


async def create_app() -> web.Application:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    validate_scheduler_modes(
        durable_scheduler_enabled=settings.planning_durable_scheduler_enabled,
        legacy_scheduler_enabled=not settings.planning_durable_scheduler_enabled,
        reminder_cutover_enabled=settings.planning_reminder_cutover_enabled,
    )
    if settings.telegram_proxy_url:
        LOGGER.info("Telegram API proxy is enabled")
    else:
        LOGGER.info("Telegram API proxy is disabled")
    LOGGER.info("Telegram mode: %s", settings.telegram_mode)
    if settings.ha_mobile_notify_services:
        LOGGER.info(
            "HA mobile notify services resolved: count=%s services=%s",
            len(settings.ha_mobile_notify_services),
            ",".join(settings.ha_mobile_notify_services),
        )
    else:
        LOGGER.warning("HA mobile notify services resolved: count=0 services=")

    session = AiohttpSession(proxy=settings.telegram_proxy_url) if settings.telegram_proxy_url else AiohttpSession()
    bot = Bot(
        token=settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(start.router)
    dispatcher.include_router(admin_modes.router)
    dispatcher.include_router(coffee.router)
    dispatcher.include_router(tea.router)
    dispatcher.include_router(water.router)
    dispatcher.include_router(reminders.router)
    if settings.planning_telegram_ui_enabled:
        dispatcher.include_router(planning.router)
    dispatcher.include_router(common.router)

    ha = HomeAssistantClient(settings.ha_url, settings.ha_long_lived_token)
    storage = MemoryStorage()
    app_state = AppStateStore(settings.app_state_path)
    coffee_timing_policy = CoffeeTimingPolicyService(
        ha,
        stale_after_seconds=settings.coffee_timing_stale_after_seconds,
    )
    try:
        await coffee_timing_policy.refresh()
        LOGGER.info("Canonical coffee timing policy loaded from Home Assistant")
    except (HomeAssistantError, TimingPolicyError):
        LOGGER.warning(
            "Canonical coffee timing policy is unavailable; timing-dependent alerts are paused"
        )
    reminder_store, planning_database = build_reminder_store(
        reminders_state_path=settings.reminders_state_path,
        planning_db_path=settings.planning_db_path,
        cutover_enabled=settings.planning_reminder_cutover_enabled,
    )
    if (
        settings.planning_api_enabled
        or settings.planning_alice_interpret_enabled
        or settings.planning_backup_enabled
        or settings.planning_icloud_enabled
    ) and planning_database is None:
        # A4/A5a have their own disabled-by-default API gates.  Opening the
        # Planning database here does not enable the A2 cutover or A3 worker.
        planning_database = PlanningDatabase(
            config=PlanningDatabaseConfig(
                path=settings.planning_db_path,
                environment=settings.planning_environment,
            )
        )
    planning_icloud_cache: ProviderCalendarCache | None = None
    planning_icloud_refresh_loop: ICloudCalendarRefreshLoop | None = None
    if planning_database is not None:
        icloud_configured = bool(
            settings.planning_icloud_account
            and settings.planning_icloud_password
            and settings.planning_icloud_caldav_url
        )
        icloud_provider = None
        if icloud_configured:
            try:
                icloud_transport = AiohttpCalDavTransport(
                    bootstrap_url=settings.planning_icloud_caldav_url,
                    username=settings.planning_icloud_account,
                    password=settings.planning_icloud_password,
                )
                icloud_provider = ICloudCalDavProvider(
                    transport=icloud_transport,
                    account_name=settings.planning_icloud_account,
                    default_timezone=settings.planning_default_timezone,
                )
            except (ValueError, RuntimeError):
                # Configuration is represented as provider-unavailable state;
                # raw values are never included in this diagnostic path.
                LOGGER.warning("iCloud calendar provider configuration is invalid")
                icloud_configured = False
        planning_icloud_cache = ProviderCalendarCache(
            planning_database,
            provider=icloud_provider,
            provider_name="icloud",
            account_id=(
                ICloudCalDavProvider.account_id_for(settings.planning_icloud_account)
                if settings.planning_icloud_account
                else None
            ),
            display_label="iCloud",
            enabled=settings.planning_icloud_enabled,
            configured=icloud_configured,
            stale_after_seconds=provider_stale_after_seconds(
                settings.planning_icloud_refresh_interval_seconds
            ),
        )
        if settings.planning_icloud_enabled and icloud_provider is not None:
            planning_icloud_refresh_loop = ICloudCalendarRefreshLoop(
                planning_icloud_cache,
                interval_seconds=settings.planning_icloud_refresh_interval_seconds,
            )
    planning_telegram_service: PlanningTelegramService | None = None
    if settings.planning_telegram_ui_enabled:
        if planning_database is None:
            raise RuntimeError("Planning Telegram UI requires the canonical Planning database")
        planning_telegram_service = PlanningTelegramService(
            planning_database,
            default_timezone=settings.planning_default_timezone,
            action_token_ttl_seconds=settings.planning_telegram_action_token_ttl_seconds,
            callback_rate_limit_per_minute=settings.planning_telegram_callback_rate_limit_per_minute,
        )
    admin_mode_manager = AdminModeManager()
    telegram_messages = TelegramMessages(bot)
    coffee_workflow = CoffeeWorkflow(settings, ha, storage, telegram_messages)
    tea_workflow = TeaWorkflow(settings, ha, storage, telegram_messages)
    water_workflow = WaterWorkflow(settings, ha, storage, telegram_messages)
    durable_reminder_scheduler: DurableReminderScheduler | None = None
    if settings.planning_durable_scheduler_enabled:
        if planning_database is None:
            raise RuntimeError("durable reminder scheduler requires a Planning database")
        durable_reminder_scheduler = DurableReminderScheduler(
            planning_database,
            telegram_transport=TelegramDeliveryTransport(telegram_messages),
            mobile_transport=HomeAssistantMobileTransport(ha, settings.ha_mobile_notify_services),
            spoken_transport=AliceSpokenDeliveryTransport(ha, reminder_store.get_settings),
            default_chat_id=settings.telegram_admin_chat_id,
            settings_provider=reminder_store.get_settings,
            interval_seconds=settings.planning_scheduler_poll_interval_seconds,
            lease_seconds=settings.planning_scheduler_lease_seconds,
            batch_size=settings.planning_scheduler_batch_size,
            jitter_bound_seconds=settings.planning_scheduler_jitter_seconds,
        )
    planning_backup_service: PlanningBackupService | None = None
    if settings.planning_backup_enabled and planning_database is not None:
        try:
            planning_backup_service = PlanningBackupService(
                planning_database,
                backup_dir=settings.planning_backup_dir,
                encryption_key=settings.planning_backup_encryption_key,
                retention_count=settings.planning_backup_retention_count,
                application_version=settings.app_version,
                application_commit=settings.app_commit,
                environment=settings.planning_environment,
            )
        except PlanningBackupError as exc:
            LOGGER.error(
                "Planning backup subsystem unavailable: code=%s category=%s",
                exc.code,
                exc.category,
            )
    planning_health_service = PlanningHealthService(
        planning_database,
        scheduler=durable_reminder_scheduler,
        scheduler_enabled=settings.planning_durable_scheduler_enabled,
        scheduler_heartbeat_stale_after_seconds=max(
            15.0,
            settings.planning_scheduler_poll_interval_seconds * 3
            + settings.planning_scheduler_jitter_seconds
            + 5.0,
        ),
        backup_dir=settings.planning_backup_dir,
        backup_enabled=settings.planning_backup_enabled,
        backup_service_ready=planning_backup_service is not None,
        backup_interval_seconds=settings.planning_backup_interval_seconds,
        application_version=settings.app_version,
        application_commit=settings.app_commit,
        state_store=planning_backup_service.state_store if planning_backup_service is not None else None,
        provider_cache=planning_icloud_cache,
    )
    reminder_workflow = ReminderWorkflow(
        reminder_store,
        telegram_messages,
        settings.telegram_admin_chat_id,
        settings,
        ha,
        durable_scheduler_enabled=settings.planning_durable_scheduler_enabled,
    )
    pushward_coffee_activity = PushWardCoffeeActivity(
        settings,
        ha,
        app_state,
        coffee_timing_policy,
    )
    pushward_coffee_widget = PushWardCoffeeWidget(
        settings,
        app_state,
        coffee_timing_policy,
    )
    coffee_alert_scheduler = CoffeeAlertScheduler(
        settings,
        app_state,
        telegram_messages,
        ha,
        coffee_timing_policy,
        pushward_coffee_activity,
        pushward_coffee_widget,
    )
    coffee_timing_refresher = CoffeeTimingPolicyRefresher(
        coffee_timing_policy,
        interval_seconds=settings.coffee_timing_refresh_interval_seconds,
        max_backoff_seconds=settings.coffee_timing_refresh_max_backoff_seconds,
        on_policy_change=lambda _: coffee_alert_scheduler.reschedule_active_alerts(),
    )
    control_center_coffee_actions = ControlCenterCoffeeActions(ha, settings)

    dispatcher["settings"] = settings
    dispatcher["ha"] = ha
    dispatcher["storage"] = storage
    dispatcher["app_state"] = app_state
    dispatcher["coffee_timing_policy"] = coffee_timing_policy
    dispatcher["admin_modes"] = admin_mode_manager
    dispatcher["telegram_messages"] = telegram_messages
    dispatcher["coffee_workflow"] = coffee_workflow
    dispatcher["tea_workflow"] = tea_workflow
    dispatcher["water_workflow"] = water_workflow
    dispatcher["reminder_workflow"] = reminder_workflow
    if planning_telegram_service is not None:
        dispatcher["planning_telegram_service"] = planning_telegram_service
    dispatcher["coffee_alert_scheduler"] = coffee_alert_scheduler
    dispatcher["pushward_coffee_activity"] = pushward_coffee_activity
    dispatcher["pushward_coffee_widget"] = pushward_coffee_widget

    app = web.Application()
    app["settings"] = settings
    app["bot"] = bot
    app["dispatcher"] = dispatcher
    app["ha"] = ha
    app["app_state"] = app_state
    app["coffee_timing_policy"] = coffee_timing_policy
    app["coffee_timing_refresher"] = coffee_timing_refresher
    app["control_center_coffee_actions"] = control_center_coffee_actions
    app["admin_modes"] = admin_mode_manager
    app["coffee_workflow"] = coffee_workflow
    app["tea_workflow"] = tea_workflow
    app["water_workflow"] = water_workflow
    app["reminder_workflow"] = reminder_workflow
    app["planning_database"] = planning_database
    app["planning_telegram_service"] = planning_telegram_service
    app["durable_reminder_scheduler"] = durable_reminder_scheduler
    app["planning_backup_service"] = planning_backup_service
    app["planning_health_service"] = planning_health_service
    app["planning_icloud_cache"] = planning_icloud_cache
    app["planning_icloud_refresh_loop"] = planning_icloud_refresh_loop
    app["coffee_alert_scheduler"] = coffee_alert_scheduler
    app["pushward_coffee_activity"] = pushward_coffee_activity
    app["pushward_coffee_widget"] = pushward_coffee_widget

    if settings.telegram_mode == "webhook":
        setup_telegram_routes(app, settings.webhook_path)
    setup_internal_routes(app)
    if planning_icloud_refresh_loop is not None:
        planning_icloud_refresh_loop.start()
    if durable_reminder_scheduler is not None:
        await durable_reminder_scheduler.start()
    else:
        await reminder_workflow.restore_pending()
    await coffee_alert_scheduler.restore()
    await pushward_coffee_widget.restore()
    coffee_timing_refresher.start()

    async def close_resources(_: web.Application) -> None:
        if planning_icloud_refresh_loop is not None:
            await planning_icloud_refresh_loop.close()
        if durable_reminder_scheduler is not None:
            await durable_reminder_scheduler.close()
        await coffee_timing_refresher.close()
        await pushward_coffee_widget.close()
        await pushward_coffee_activity.close()
        await ha.close()
        await bot.session.close()
        if planning_database is not None:
            planning_database.close()

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
