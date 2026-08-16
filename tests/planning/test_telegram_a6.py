from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.handlers.planning import planning_action_handler, planning_menu_handler
from app.keyboards.coffee import delete_only
from app.keyboards.main import main_menu
from app.messages import planning as planning_messages
from app.planning import (
    MutationContext,
    PlanningDatabase,
    PlanningNotFoundError,
    PlanningRepository,
    PlanningTelegramService,
    PlanningValidationError,
    PlanningVersionConflictError,
    TelegramActionTokenConsumedError,
    TelegramActionTokenExpiredError,
    TelegramActionTokenStore,
    TelegramActionTokenUnknownError,
)
from app.planning.delivery import TelegramDeliveryTransport
from app.planning.errors import TelegramActionTokenBindingError
from app.planning.telegram_actions import TelegramMutationRateLimiter, encode_action_callback


NOW = "2026-08-12T08:00:00.000000Z"
CONTEXT = MutationContext(
    audience="operator",
    actor_id="a6-fixture",
    actor_type="operator",
    surface="operator",
)


class A6Clock:
    def __init__(self, value: str = NOW) -> None:
        self.value = datetime.fromisoformat(value[:-1] + "+00:00")

    def __call__(self) -> str:
        return self.value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, **kwargs: int) -> str:
        self.value += timedelta(**kwargs)
        return self()


class FakeTelegramMessages:
    def __init__(self) -> None:
        self.edits: list[tuple[int, int, str, object]] = []

    async def safe_edit(self, chat_id: int, message_id: int, text: str, reply_markup: object) -> int:
        self.edits.append((chat_id, message_id, text, reply_markup))
        return message_id


class FakeCallback:
    def __init__(self, user_id: int, chat_id: int = 42, data: str | None = None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=7)
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class AdminSettings:
    planning_telegram_ui_enabled = True

    @staticmethod
    def is_admin_user(user_id: int | None) -> bool:
        return user_id == 1


class A6TelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "planning.sqlite3"
        self.clock = A6Clock()
        self.database = PlanningDatabase(self.path)
        self.repository = PlanningRepository(self.database, now_fn=self.clock)
        self.service = PlanningTelegramService(
            self.database,
            repository=self.repository,
            default_timezone="Europe/Moscow",
            now_fn=self.clock,
            rate_now_fn=lambda: 1.0,
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def reminder(self, *, title: str = "Reminder", due_at: str = NOW, chat_id: int = 42):
        return self.repository.create_reminder(
            title=title,
            due_at_utc=due_at,
            timezone="Europe/Moscow",
            context=CONTEXT,
            outbox_job_type="planning.reminder.delivery.v1",
            outbox_payload={"chat_id": chat_id},
        )

    def test_schema_adds_persistent_token_store_and_reopens(self) -> None:
        self.assertEqual(self.database.schema_version(), 5)
        reminder = self.reminder()
        issued = self.service.issue_reminder_complete_token(
            reminder=reminder,
            telegram_user_id=1,
            telegram_chat_id=42,
        )
        raw = issued.removeprefix("planning:a:")
        rows = self.database.connection.execute("SELECT * FROM telegram_action_tokens").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(raw, dict(rows[0]).values())
        self.database.close()
        self.database = PlanningDatabase(self.path)
        store = TelegramActionTokenStore(self.database, now_fn=self.clock)
        result = store.consume(
            issued,
            telegram_user_id=1,
            telegram_chat_id=42,
            mutation=lambda token: token.action,
        )
        self.assertEqual(result, "reminder_complete")

    def test_expired_token_cleanup_is_bounded(self) -> None:
        first = self.reminder(title="first")
        second = self.reminder(title="second")
        self.service.issue_reminder_complete_token(reminder=first, telegram_user_id=1, telegram_chat_id=42)
        self.service.issue_reminder_complete_token(reminder=second, telegram_user_id=1, telegram_chat_id=42)
        removed = self.service.action_tokens.cleanup_expired(
            now="2026-08-12T08:16:00.000000Z",
            limit=1,
        )
        self.assertEqual(removed, 1)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM telegram_action_tokens").fetchone()[0],
            1,
        )

    def test_token_entropy_callback_limit_and_no_user_text(self) -> None:
        reminder = self.reminder(title="<script> секретное название")
        view = self.service.reminders_view(telegram_user_id=1, telegram_chat_id=42)
        callbacks = [button.callback_data for row in view.rows for button in row]
        mutation_callbacks = [value for value in callbacks if value.startswith("planning:a:")]
        self.assertTrue(mutation_callbacks)
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in mutation_callbacks))
        self.assertTrue(all(len(value.removeprefix("planning:a:")) >= 32 for value in mutation_callbacks))
        self.assertTrue(all("script" not in value and reminder.id not in value for value in mutation_callbacks))
        self.assertEqual(encode_action_callback("A" * 43), "planning:a:" + "A" * 43)
        with self.assertRaises(ValueError):
            encode_action_callback("A" * 44)

    def test_unknown_tampered_expired_and_consumed_tokens_are_rejected(self) -> None:
        reminder = self.reminder()
        issued = self.service.issue_reminder_complete_token(
            reminder=reminder,
            telegram_user_id=1,
            telegram_chat_id=42,
        )
        with self.assertRaises(TelegramActionTokenUnknownError):
            self.service.execute_action("planning:a:" + "A" * 43, telegram_user_id=1, telegram_chat_id=42)
        with self.assertRaises(TelegramActionTokenUnknownError):
            self.service.execute_action(issued[:-1] + "X", telegram_user_id=1, telegram_chat_id=42)
        with self.assertRaises(TelegramActionTokenExpiredError):
            self.service.execute_action(
                issued,
                telegram_user_id=1,
                telegram_chat_id=42,
                now="2026-08-12T08:16:00.000000Z",
            )
        result = self.service.execute_action(issued, telegram_user_id=1, telegram_chat_id=42)
        self.assertEqual(result.action, "reminder_complete")
        with self.assertRaises(TelegramActionTokenConsumedError):
            self.service.execute_action(issued, telegram_user_id=1, telegram_chat_id=42)

    def test_user_and_chat_binding_are_enforced(self) -> None:
        reminder = self.reminder()
        issued = self.service.issue_reminder_cancel_token(
            reminder=reminder,
            telegram_user_id=1,
            telegram_chat_id=42,
        )
        with self.assertRaises(TelegramActionTokenBindingError) as user_error:
            self.service.execute_action(issued, telegram_user_id=2, telegram_chat_id=42)
        self.assertEqual(user_error.exception.reason, "wrong_user")
        with self.assertRaises(TelegramActionTokenBindingError) as chat_error:
            self.service.execute_action(issued, telegram_user_id=1, telegram_chat_id=43)
        self.assertEqual(chat_error.exception.reason, "wrong_chat")
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "pending")

    def test_atomic_rollback_does_not_consume_token(self) -> None:
        reminder = self.reminder()
        issued = self.service.issue_reminder_complete_token(
            reminder=reminder,
            telegram_user_id=1,
            telegram_chat_id=42,
        )
        with self.assertRaises(RuntimeError):
            self.service.action_tokens.consume(
                issued,
                telegram_user_id=1,
                telegram_chat_id=42,
                mutation=lambda _token: (_ for _ in ()).throw(RuntimeError("fixture rollback")),
            )
        row = self.database.connection.execute(
            "SELECT consumed_at FROM telegram_action_tokens"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "pending")

    def test_concurrent_double_click_allows_one_mutation(self) -> None:
        reminder = self.reminder()
        issued = self.service.issue_reminder_complete_token(
            reminder=reminder,
            telegram_user_id=1,
            telegram_chat_id=42,
        )
        other_database = PlanningDatabase(self.path)
        other_service = PlanningTelegramService(other_database, now_fn=self.clock, rate_now_fn=lambda: 2.0)

        def click(service: PlanningTelegramService):
            try:
                return service.execute_action(issued, telegram_user_id=1, telegram_chat_id=42)
            except Exception as exc:  # one expected replay rejection
                return exc

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(click, (self.service, other_service)))
        finally:
            other_database.close()
        self.assertEqual(sum(isinstance(value, Exception) for value in outcomes), 1)
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "completed")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM telegram_action_tokens WHERE consumed_at IS NOT NULL"
            ).fetchone()[0],
            1,
        )

    def test_reminder_views_keep_delivered_open_and_exclude_terminal_statuses(self) -> None:
        future = self.reminder(title="future", due_at="2026-08-13T08:00:00Z")
        queued = self.reminder(title="queued", due_at="2026-08-12T06:00:00Z")
        retrying = self.reminder(title="retrying", due_at="2026-08-12T06:01:00Z")
        delivered = self.reminder(title="delivered open", due_at="2026-08-12T06:02:00Z")
        failed = self.reminder(title="failed", due_at="2026-08-12T06:03:00Z")
        completed = self.reminder(title="completed", due_at="2026-08-12T06:04:00Z")
        cancelled = self.reminder(title="cancelled", due_at="2026-08-12T06:05:00Z")
        for reminder, state in ((queued, "queued"), (retrying, "retrying"), (delivered, "delivered"), (failed, "failed")):
            reminder = self.repository.update_reminder(
                reminder.id,
                expected_version=reminder.version,
                context=CONTEXT,
                status="due",
                delivery_state=state,
                final_failure_at=NOW if state == "failed" else None,
            )
        self.repository.complete_reminder(completed.id, expected_version=completed.version, context=CONTEXT)
        self.repository.cancel_reminder(cancelled.id, expected_version=cancelled.version, context=CONTEXT)
        view = self.service.reminders_view(telegram_user_id=1, telegram_chat_id=42)
        for title in ("future", "queued", "retrying", "delivered open", "failed"):
            self.assertIn(title, view.text)
        self.assertIn("доставлено · ждёт выполнения", view.text)
        self.assertIn("доставка не удалась", view.text)
        self.assertNotIn("completed", view.text)
        self.assertNotIn("cancelled", view.text)
        self.assertLess(view.text.index("queued"), view.text.index("future"))

    def test_reminder_complete_cancel_and_manual_retry_preserve_attempt_history(self) -> None:
        reminder = self.reminder(title="retryable", due_at=NOW)
        attempt = self.repository.start_delivery_attempt(reminder_id=reminder.id, channel="telegram", started_at=NOW)
        self.repository.finish_delivery_attempt(
            attempt_id=attempt.id,
            status="failed",
            finished_at=NOW,
            error_code="telegram_timeout",
        )
        failed = self.repository.update_reminder(
            reminder.id,
            expected_version=reminder.version,
            context=CONTEXT,
            status="due",
            delivery_state="failed",
            final_failure_at=NOW,
        )
        retry_token = self.service.issue_reminder_retry_token(
            reminder=failed, telegram_user_id=1, telegram_chat_id=42
        )
        retry_result = self.service.execute_action(retry_token, telegram_user_id=1, telegram_chat_id=42)
        self.assertEqual(retry_result.action, "reminder_retry")
        retried = self.repository.get_reminder(reminder.id)
        self.assertEqual((retried.status, retried.delivery_state), ("due", "queued"))
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 1)
        complete_token = self.service.issue_reminder_complete_token(
            reminder=retried, telegram_user_id=1, telegram_chat_id=42
        )
        self.service.execute_action(complete_token, telegram_user_id=1, telegram_chat_id=42)
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "completed")

        other = self.reminder(title="cancel me")
        cancel_token = self.service.issue_reminder_cancel_token(
            reminder=other, telegram_user_id=1, telegram_chat_id=42
        )
        self.service.execute_action(cancel_token, telegram_user_id=1, telegram_chat_id=42)
        self.assertEqual(self.repository.get_reminder(other.id).status, "cancelled")

    def test_tasks_views_and_date_only_semantics(self) -> None:
        today = self.repository.create_task(title="today date-only", due_date="2026-08-12", context=CONTEXT)
        timed = self.repository.create_task(
            title="today timed", due_date="2026-08-12", due_time="09:30", timezone="Europe/Moscow", context=CONTEXT
        )
        overdue = self.repository.create_task(title="overdue", due_date="2026-08-11", context=CONTEXT)
        upcoming = self.repository.create_task(title="upcoming", due_date="2026-08-13", context=CONTEXT)
        done = self.repository.create_task(title="completed hidden", due_date="2026-08-12", context=CONTEXT)
        archived = self.repository.create_task(title="archived hidden", due_date="2026-08-12", context=CONTEXT)
        self.repository.complete_task(done.id, expected_version=done.version, context=CONTEXT)
        self.repository.archive_task(archived.id, expected_version=archived.version, context=CONTEXT)

        today_view = self.service.tasks_view(view="today", telegram_user_id=1, telegram_chat_id=42)
        self.assertIn("today date-only", today_view.text)
        self.assertIn("12.08.2026 · весь день", today_view.text)
        self.assertIn("today timed", today_view.text)
        self.assertIn("09:30", today_view.text)
        self.assertNotIn("completed hidden", today_view.text)
        self.assertNotIn("00:00", today_view.text)
        self.assertIn("planning:tasks:overdue:0", [button.callback_data for row in today_view.rows for button in row])
        self.assertIn("overdue", self.service.tasks_view(view="overdue", telegram_user_id=1, telegram_chat_id=42).text)
        self.assertIn("upcoming", self.service.tasks_view(view="upcoming", telegram_user_id=1, telegram_chat_id=42).text)

        # The canonical repository orders timed tasks before date-only tasks;
        # the first action therefore belongs to the timed task.
        token = today_view.rows[0][0].callback_data
        outcome = self.service.execute_action(token, telegram_user_id=1, telegram_chat_id=42)
        self.assertEqual(outcome.action, "task_complete")
        self.assertEqual(self.repository.get_task(timed.id).status, "completed")

        stale_task = self.repository.create_task(
            title="stale task",
            due_date="2026-08-12",
            due_time="11:00",
            timezone="Europe/Moscow",
            context=CONTEXT,
        )
        stale_token = self.service.issue_task_complete_token(
            task=stale_task,
            telegram_user_id=1,
            telegram_chat_id=42,
        )
        self.repository.update_task(
            stale_task.id,
            expected_version=stale_task.version,
            context=CONTEXT,
            title="changed",
        )
        with self.assertRaises(PlanningVersionConflictError):
            self.service.execute_action(stale_token, telegram_user_id=1, telegram_chat_id=42)

    def test_event_views_local_day_all_day_first_exclusive_end_and_local_only_label(self) -> None:
        self.repository.create_calendar_event(
            title="all-day first",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-12",
            end_date_exclusive="2026-08-13",
            context=CONTEXT,
        )
        self.repository.create_calendar_event(
            title="timed second",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-12T07:00:00Z",
            end_at_utc="2026-08-12T08:00:00Z",
            context=CONTEXT,
        )
        self.repository.create_calendar_event(
            title="tomorrow event",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-13",
            end_date_exclusive="2026-08-14",
            context=CONTEXT,
        )
        today_view = self.service.events_view(view="today")
        self.assertLess(today_view.text.index("all-day first"), today_view.text.index("timed second"))
        self.assertIn("локально · не синхронизировано", today_view.text)
        self.assertIn("12.08.2026 · весь день", today_view.text)
        tomorrow_view = self.service.events_view(view="tomorrow")
        self.assertIn("tomorrow event", tomorrow_view.text)
        self.assertNotIn("all-day first", tomorrow_view.text)
        self.assertIn("planning:events:upcoming:0", [button.callback_data for row in tomorrow_view.rows for button in row])

    def test_malformed_navigation_and_rate_limiter_are_bounded(self) -> None:
        from app.planning.telegram_ui import parse_navigation_callback

        self.assertIsNone(parse_navigation_callback("planning:tasks:today:-1"))
        self.assertIsNone(parse_navigation_callback("planning:tasks:today:1:extra"))
        with self.assertRaises(ValueError):
            parse_navigation_callback("planning:tasks:today:999")
        limiter_clock = iter((0.0, 1.0, 2.0, 61.0))
        limiter = TelegramMutationRateLimiter(limit=2, now_fn=lambda: next(limiter_clock))
        self.assertTrue(limiter.allow(1))
        self.assertTrue(limiter.allow(1))
        self.assertFalse(limiter.allow(1))
        self.assertTrue(limiter.allow(1))

    def test_synthetic_handler_rechecks_admin_and_refreshes_after_action(self) -> None:
        reminder = self.reminder()
        view = self.service.reminders_view(telegram_user_id=1, telegram_chat_id=42)
        token = view.rows[0][0].callback_data
        messages = FakeTelegramMessages()

        async def run() -> None:
            unauthorized = FakeCallback(2, data="planning:menu")
            await planning_menu_handler(unauthorized, AdminSettings(), self.service, messages)
            self.assertEqual(unauthorized.answers[-1][0], "Нет доступа")

            authorized = FakeCallback(1, data=token)
            await planning_action_handler(authorized, AdminSettings(), self.service, messages)
            self.assertEqual(self.repository.get_reminder(reminder.id).status, "completed")
            self.assertTrue(messages.edits)
            self.assertEqual(authorized.answers[-1][0], "Напоминание выполнено.")

        asyncio.run(run())

    def test_disabled_settings_and_invalid_authority_fail_closed(self) -> None:
        baseline = {
            "TELEGRAM_BOT_TOKEN": "bot",
            "TELEGRAM_WEBHOOK_SECRET": "webhook",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
            "TELEGRAM_ADMIN_CHAT_ID": "42",
            "HA_LONG_LIVED_TOKEN": "ha",
            "INTERNAL_WEBHOOK_SECRET": "internal",
        }
        with patch.dict(os.environ, baseline, clear=True):
            settings = Settings.from_env()
            self.assertFalse(settings.planning_telegram_ui_enabled)
            self.assertFalse(settings.planning_reminder_cutover_enabled)
        with patch.dict(
            os.environ,
            {**baseline, "PLANNING_TELEGRAM_UI_ENABLED": "true", "PLANNING_REMINDER_CUTOVER_ENABLED": "true"},
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                Settings.from_env()

    def test_main_menu_only_exposes_planning_when_enabled(self) -> None:
        disabled_callbacks = [
            button.callback_data
            for row in main_menu().inline_keyboard
            for button in row
        ]
        enabled_callbacks = [
            button.callback_data
            for row in main_menu(planning_enabled=True).inline_keyboard
            for button in row
        ]
        self.assertNotIn("planning:menu", disabled_callbacks)
        self.assertIn("planning:menu", enabled_callbacks)


class A6DeliveryMarkupTests(unittest.IsolatedAsyncioTestCase):
    async def test_a3_delivery_remains_delete_only_and_does_not_use_a6_token(self) -> None:
        class FakeMessages:
            def __init__(self) -> None:
                self.markup = None

            async def send_delivery(self, chat_id: int, text: str, reply_markup=None) -> int:
                self.markup = reply_markup
                return 99

        fake = FakeMessages()
        transport = TelegramDeliveryTransport(fake)  # type: ignore[arg-type]
        reminder = SimpleNamespace(title="delivery fixture")
        result = await transport.send(reminder=reminder, chat_id=42, correlation_id="ignored")  # type: ignore[arg-type]
        self.assertEqual(result.kind, "success")
        callbacks = [
            button.callback_data
            for row in fake.markup.inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["message:delete"])
        self.assertFalse(any(value.startswith("planning:a:") for value in callbacks))


if __name__ == "__main__":
    unittest.main()
