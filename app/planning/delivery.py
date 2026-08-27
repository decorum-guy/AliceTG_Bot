from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

from app.keyboards.coffee import delete_only
from app.messages import reminders as reminder_messages
from app.planning.models import Reminder
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.reminder_store import ReminderSettings
from app.services.telegram_messages import TelegramMessages


DeliveryKind = Literal["success", "retryable", "permanent"]


@dataclass(frozen=True)
class DeliveryResult:
    """Provider-neutral result for one channel attempt."""

    kind: DeliveryKind
    code: str
    diagnostic: str | None = None
    provider_receipt: str | None = None
    retry_after_seconds: int | None = None

    @classmethod
    def success(cls, *, provider_receipt: str | None = None, code: str = "delivered") -> "DeliveryResult":
        return cls("success", code, provider_receipt=provider_receipt)

    @classmethod
    def retryable(
        cls,
        code: str,
        *,
        diagnostic: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> "DeliveryResult":
        return cls(
            "retryable",
            code,
            diagnostic=_bounded_diagnostic(diagnostic),
            retry_after_seconds=retry_after_seconds,
        )

    @classmethod
    def permanent(cls, code: str, *, diagnostic: str | None = None) -> "DeliveryResult":
        return cls("permanent", code, diagnostic=_bounded_diagnostic(diagnostic))


class ReminderChannelTransport(Protocol):
    channel: str

    async def send(
        self,
        *,
        reminder: Reminder,
        chat_id: int,
        correlation_id: str,
    ) -> DeliveryResult: ...


def _bounded_diagnostic(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized[:256] if normalized else None


class TelegramDeliveryTransport:
    """A narrow adapter over the bot's existing TelegramMessages client."""

    channel = "telegram"

    def __init__(self, telegram_messages: TelegramMessages) -> None:
        self._telegram_messages = telegram_messages

    async def send(
        self,
        *,
        reminder: Reminder,
        chat_id: int,
        correlation_id: str,
    ) -> DeliveryResult:
        del correlation_id  # Correlation is persisted in the attempt row.
        try:
            message_id = await self._telegram_messages.send_delivery(
                chat_id,
                reminder_messages.reminder_notification(reminder.title),
                reply_markup=delete_only(),
            )
        except asyncio.TimeoutError:
            return DeliveryResult.retryable("telegram_timeout", diagnostic="Telegram request timed out")
        except (TelegramRetryAfter,) as exc:
            return DeliveryResult.retryable(
                "telegram_rate_limited",
                diagnostic="Telegram requested a retry",
                retry_after_seconds=max(0, int(exc.retry_after)),
            )
        except (TelegramNetworkError, OSError):
            return DeliveryResult.retryable("telegram_network", diagnostic="Telegram network failure")
        except TelegramServerError:
            return DeliveryResult.retryable("telegram_5xx", diagnostic="Telegram temporary provider failure")
        except TelegramForbiddenError:
            return DeliveryResult.permanent("telegram_forbidden", diagnostic="Telegram chat is blocked or forbidden")
        except TelegramNotFound:
            return DeliveryResult.permanent("telegram_chat_not_found", diagnostic="Telegram chat was not found")
        except TelegramUnauthorizedError:
            return DeliveryResult.permanent("telegram_unauthorized", diagnostic="Telegram bot authorization failed")
        except TelegramBadRequest as exc:
            # aiogram exposes chat-not-found as a typed bad request in some
            # Bot API versions.  Only use the exception text for this narrow
            # subtype; all other diagnostics stay static and redacted.
            lowered = str(exc).lower()
            if "chat not found" in lowered:
                return DeliveryResult.permanent(
                    "telegram_chat_not_found",
                    diagnostic="Telegram chat was not found",
                )
            return DeliveryResult.permanent("telegram_bad_request", diagnostic="Telegram rejected the message")
        except TelegramAPIError:
            return DeliveryResult.retryable("telegram_provider_error", diagnostic="Telegram provider failure")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A provider-library/runtime failure is treated conservatively as
            # retryable; the raw exception is intentionally not persisted.
            return DeliveryResult.retryable("telegram_transport_error", diagnostic="Telegram transport failure")

        if message_id is None:
            return DeliveryResult.retryable("telegram_no_receipt", diagnostic="Telegram returned no message receipt")
        return DeliveryResult.success(provider_receipt=f"message:{int(message_id)}")


class HomeAssistantMobileTransport:
    """Optional mobile notification adapter restricted to configured services."""

    channel = "iphone"

    def __init__(self, home_assistant: HomeAssistantClient, services: tuple[str, ...]) -> None:
        self._home_assistant = home_assistant
        self._services = services

    async def send(
        self,
        *,
        reminder: Reminder,
        chat_id: int,
        correlation_id: str,
    ) -> DeliveryResult:
        del chat_id, correlation_id
        if not self._services:
            return DeliveryResult.permanent("ha_mobile_not_configured", diagnostic="No configured mobile service")

        successful_services = 0
        retryable_failures = 0
        permanent_failures = 0
        for service in self._services:
            try:
                await self._home_assistant.notify(
                    service,
                    title="Напоминание",
                    message=reminder.title,
                )
            except asyncio.TimeoutError:
                retryable_failures += 1
            except HomeAssistantError as exc:
                if exc.status is None or exc.status == 429 or exc.status >= 500:
                    retryable_failures += 1
                else:
                    permanent_failures += 1
            except (OSError, RuntimeError):
                retryable_failures += 1
            else:
                successful_services += 1

        if successful_services:
            return DeliveryResult.success(code="ha_mobile_delivered", provider_receipt="ha-mobile")
        if retryable_failures:
            return DeliveryResult.retryable(
                "ha_mobile_temporary_failure",
                diagnostic="Home Assistant mobile services temporarily failed",
            )
        if permanent_failures:
            return DeliveryResult.permanent(
                "ha_mobile_permanent_failure",
                diagnostic="Home Assistant mobile services rejected the notification",
            )
        return DeliveryResult.permanent("ha_mobile_failed", diagnostic="Home Assistant mobile delivery failed")


class AliceSpokenDeliveryTransport:
    """Spoken Alice delivery through the already configured HA media player."""

    channel = "alice"

    def __init__(
        self,
        home_assistant: HomeAssistantClient,
        settings_provider: Callable[[], Awaitable[ReminderSettings]],
    ) -> None:
        self._home_assistant = home_assistant
        self._settings_provider = settings_provider

    async def send(
        self,
        *,
        reminder: Reminder,
        chat_id: int,
        correlation_id: str,
    ) -> DeliveryResult:
        del chat_id, correlation_id
        settings = await self._settings_provider()
        if not settings.voice_enabled:
            return DeliveryResult.permanent("alice_voice_disabled", diagnostic="Alice spoken delivery is disabled")
        station = settings.voice_station_entity_id.strip()
        if not station:
            return DeliveryResult.permanent("alice_not_configured", diagnostic="Alice voice station is not configured")
        try:
            await self._home_assistant.play_media(station, reminder.title)
        except asyncio.TimeoutError:
            return DeliveryResult.retryable("alice_timeout", diagnostic="Alice voice request timed out")
        except HomeAssistantError as exc:
            if exc.status is None or exc.status == 429 or exc.status >= 500:
                return DeliveryResult.retryable("alice_temporary_failure", diagnostic="Alice voice service temporarily failed")
            return DeliveryResult.permanent("alice_rejected", diagnostic="Home Assistant rejected Alice voice delivery")
        except (OSError, RuntimeError):
            return DeliveryResult.retryable("alice_transport_error", diagnostic="Alice voice transport failed")
        return DeliveryResult.success(code="alice_spoken_delivered", provider_receipt="ha-media-player")
