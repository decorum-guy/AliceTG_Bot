from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from app.planning.delivery import HomeAssistantMobileTransport, TelegramDeliveryTransport
from app.services.home_assistant import HomeAssistantError


class FakeTelegramMessages:
    def __init__(self, outcome):
        self.outcome = outcome

    async def send_delivery(self, *args, **kwargs):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeHomeAssistant:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [None])
        self.services = []

    async def notify(self, service, *, title, message):
        self.services.append(service)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome


class DeliveryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_typed_failures_are_classified_without_raw_body_persistence(self) -> None:
        method = SendMessage(chat_id=1, text="synthetic")
        cases = [
            (asyncio.TimeoutError(), "telegram_timeout", "retryable"),
            (TelegramNetworkError(method, "private network body"), "telegram_network", "retryable"),
            (TelegramRetryAfter(method, "private rate body", 42), "telegram_rate_limited", "retryable"),
            (TelegramServerError(method, "private server body"), "telegram_5xx", "retryable"),
            (TelegramForbiddenError(method, "private blocked body"), "telegram_forbidden", "permanent"),
            (TelegramNotFound(method, "private not found body"), "telegram_chat_not_found", "permanent"),
            (TelegramBadRequest(method, "chat not found: private id"), "telegram_chat_not_found", "permanent"),
        ]
        for error, code, kind in cases:
            with self.subTest(code=code):
                result = await TelegramDeliveryTransport(FakeTelegramMessages(error)).send(
                    reminder=SimpleNamespace(title="Synthetic reminder"),
                    chat_id=1,
                    correlation_id="correlation",
                )
                self.assertEqual((result.kind, result.code), (kind, code))
                self.assertNotIn("private", result.diagnostic or "")
                if code == "telegram_rate_limited":
                    self.assertEqual(result.retry_after_seconds, 42)

    async def test_optional_mobile_transport_uses_only_configured_allowlist(self) -> None:
        home_assistant = FakeHomeAssistant()
        transport = HomeAssistantMobileTransport(home_assistant, ("notify.mobile_fixture",))
        result = await transport.send(
            reminder=SimpleNamespace(title="Synthetic reminder"),
            chat_id=1,
            correlation_id="correlation",
        )
        self.assertEqual(result.kind, "success")
        self.assertEqual(home_assistant.services, ["notify.mobile_fixture"])

    async def test_optional_mobile_temporary_failure_is_independent(self) -> None:
        home_assistant = FakeHomeAssistant([HomeAssistantError("private body", status=503)])
        transport = HomeAssistantMobileTransport(home_assistant, ("notify.mobile_fixture",))
        result = await transport.send(
            reminder=SimpleNamespace(title="Synthetic reminder"),
            chat_id=1,
            correlation_id="correlation",
        )
        self.assertEqual((result.kind, result.code), ("retryable", "ha_mobile_temporary_failure"))
        self.assertNotIn("private", result.diagnostic or "")


if __name__ == "__main__":
    unittest.main()
