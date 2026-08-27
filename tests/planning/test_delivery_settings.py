import asyncio
import tempfile
import unittest
from pathlib import Path

from app.planning.db import PlanningDatabase
from app.planning.delivery import DeliveryResult
from app.planning.delivery_settings import ReminderDeliveryPreferencesStore
from app.planning.errors import PlanningVersionConflictError
from app.planning.models import MutationContext
from app.planning.repositories import PlanningRepository
from app.planning.scheduler import DurableReminderScheduler
from app.services.reminder_store import ReminderSettings


CONTEXT = MutationContext(
    audience="operator",
    actor_id="test",
    actor_type="service",
    surface="system",
)


class FakeTransport:
    def __init__(self, channel: str, *results: DeliveryResult) -> None:
        self.channel = channel
        self.results = iter(results)
        self.calls = 0

    async def send(self, **_kwargs):
        self.calls += 1
        return next(self.results)


class ReminderDeliverySettingsTests(unittest.TestCase):
    def test_policy_is_sqlite_backed_and_revision_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            database = PlanningDatabase(str(Path(directory) / "planning.sqlite3"))
            store = ReminderDeliveryPreferencesStore(database)
            initial = store.ensure_from_legacy(
                spoken_endpoint="alice",
                notify_telegram_enabled=True,
                notify_iphone_enabled=False,
            )
            self.assertEqual(initial.phone_channels, ("telegram",))
            updated = store.update(
                expected_revision=initial.revision,
                spoken_endpoint="jarvis",
                phone_channels=("telegram", "home_assistant"),
                context=CONTEXT,
            )
            self.assertEqual(updated.revision, 1)
            self.assertEqual(store.get().spoken_endpoint, "jarvis")
            with self.assertRaises(PlanningVersionConflictError) as raised:
                store.update(
                    expected_revision=0,
                    spoken_endpoint="alice",
                    phone_channels=("telegram",),
                    context=CONTEXT,
                )
            self.assertIn("expected version", str(raised.exception))
            database.close()


class ReminderDeliverySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = PlanningDatabase(str(Path(self.directory.name) / "planning.sqlite3"))
        self.repository = PlanningRepository(self.database)
        self.reminder = self.repository.create_reminder(
            title="Test reminder",
            due_at_utc="2026-08-27T00:00:00Z",
            timezone="UTC",
            context=CONTEXT,
            outbox_job_type="planning.reminder.delivery.v1",
            outbox_payload={"chat_id": 1},
        )

    async def asyncTearDown(self):
        self.database.close()
        self.directory.cleanup()

    async def test_each_selected_channel_retries_independently(self):
        alice = FakeTransport("alice", DeliveryResult.success())
        telegram = FakeTransport("telegram", DeliveryResult.success())
        mobile = FakeTransport("iphone", DeliveryResult.retryable("temporary"), DeliveryResult.success())
        settings = lambda: asyncio.sleep(
            0,
            result=ReminderSettings(
                spoken_endpoint="alice",
                phone_channels=("telegram", "home_assistant"),
            ),
        )
        now = ["2026-08-27T00:00:00Z"]
        scheduler = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram,
            mobile_transport=mobile,
            spoken_transport=alice,
            default_chat_id=1,
            settings_provider=settings,
            interval_seconds=5,
            jitter_bound_seconds=0,
            now_fn=lambda: now[0],
        )

        await scheduler.run_once(now[0])
        self.assertEqual(self.repository.get_reminder(self.reminder.id).delivery_state, "retrying")
        now[0] = "2026-08-27T00:00:30Z"
        await scheduler.run_once(now[0])
        self.assertEqual(self.repository.get_reminder(self.reminder.id).delivery_state, "delivered")
        self.assertEqual((alice.calls, telegram.calls, mobile.calls), (1, 1, 2))

    async def test_jarvis_unavailable_does_not_fallback_to_alice(self):
        alice = FakeTransport("alice", DeliveryResult.success())
        telegram = FakeTransport("telegram", DeliveryResult.success())
        settings = lambda: asyncio.sleep(
            0,
            result=ReminderSettings(spoken_endpoint="jarvis", phone_channels=("telegram",)),
        )
        scheduler = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram,
            mobile_transport=None,
            spoken_transport=alice,
            default_chat_id=1,
            settings_provider=settings,
            interval_seconds=5,
            jitter_bound_seconds=0,
            now_fn=lambda: "2026-08-27T00:00:00Z",
        )
        await scheduler.run_once("2026-08-27T00:00:00Z")
        self.assertEqual(alice.calls, 0)
        self.assertEqual(telegram.calls, 1)
        self.assertEqual(self.repository.get_reminder(self.reminder.id).delivery_state, "failed")
        error_codes = {
            row["error_code"]
            for row in self.database.connection.execute(
                "SELECT error_code FROM delivery_attempts WHERE reminder_id = ?",
                (self.reminder.id,),
            ).fetchall()
        }
        self.assertIn("jarvis_runtime_unavailable", error_codes)

    async def test_permanent_jarvis_does_not_cancel_telegram_retry_across_restart(self):
        alice = FakeTransport("alice", DeliveryResult.success())
        telegram = FakeTransport("telegram", DeliveryResult.retryable("telegram_timeout"))
        settings = lambda: asyncio.sleep(
            0,
            result=ReminderSettings(spoken_endpoint="jarvis", phone_channels=("telegram",)),
        )
        scheduler = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram,
            mobile_transport=None,
            spoken_transport=alice,
            default_chat_id=1,
            settings_provider=settings,
            interval_seconds=5,
            jitter_bound_seconds=0,
            now_fn=lambda: "2026-08-27T00:00:00Z",
        )

        await scheduler.run_once("2026-08-27T00:00:00Z")
        first = self.repository.get_reminder(self.reminder.id)
        self.assertEqual(first.delivery_state, "retrying")
        self.assertEqual((alice.calls, telegram.calls), (0, 1))
        outbox_row = self.database.connection.execute(
            "SELECT id FROM outbox WHERE reminder_id = ?", (self.reminder.id,)
        ).fetchone()
        self.assertEqual(self.repository.get_outbox(str(outbox_row["id"])).status, "queued")
        self.assertEqual(
            self.repository.get_outbox(str(outbox_row["id"])).payload["delivery_terminal_channels"],
            {"jarvis": "jarvis_runtime_unavailable"},
        )

        self.database.close()
        self.database = PlanningDatabase(str(Path(self.directory.name) / "planning.sqlite3"))
        self.repository = PlanningRepository(self.database)
        telegram_after_restart = FakeTransport("telegram", DeliveryResult.success())
        restarted = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram_after_restart,
            mobile_transport=None,
            spoken_transport=alice,
            default_chat_id=1,
            settings_provider=settings,
            interval_seconds=5,
            jitter_bound_seconds=0,
            now_fn=lambda: "2026-08-27T00:00:30Z",
        )

        await restarted.run_once("2026-08-27T00:00:30Z")
        final = self.repository.get_reminder(self.reminder.id)
        self.assertEqual(final.delivery_state, "failed")
        self.assertNotIn(final.status, {"completed", "cancelled"})
        self.assertEqual((alice.calls, telegram_after_restart.calls), (0, 1))
        self.assertEqual(self.repository.get_outbox(str(outbox_row["id"])).last_error_code, "jarvis_runtime_unavailable")
        attempts = self.database.connection.execute(
            "SELECT channel, COUNT(*) AS count FROM delivery_attempts "
            "WHERE reminder_id = ? GROUP BY channel ORDER BY channel",
            (self.reminder.id,),
        ).fetchall()
        self.assertEqual([(row["channel"], row["count"]) for row in attempts], [("jarvis", 1), ("telegram", 2)])

    async def test_permanent_phone_channel_does_not_cancel_other_retry_or_resend_success(self):
        alice = FakeTransport("alice", DeliveryResult.success())
        telegram = FakeTransport("telegram", DeliveryResult.retryable("telegram_timeout"), DeliveryResult.success())
        settings = lambda: asyncio.sleep(
            0,
            result=ReminderSettings(
                spoken_endpoint="alice",
                phone_channels=("telegram", "home_assistant"),
            ),
        )
        scheduler = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram,
            mobile_transport=None,
            spoken_transport=alice,
            default_chat_id=1,
            settings_provider=settings,
            interval_seconds=5,
            jitter_bound_seconds=0,
            now_fn=lambda: "2026-08-27T00:00:00Z",
        )

        await scheduler.run_once("2026-08-27T00:00:00Z")
        self.assertEqual(self.repository.get_reminder(self.reminder.id).delivery_state, "retrying")
        await scheduler.run_once("2026-08-27T00:00:30Z")
        self.assertEqual(self.repository.get_reminder(self.reminder.id).delivery_state, "failed")
        self.assertEqual((alice.calls, telegram.calls), (1, 2))
        attempts = self.database.connection.execute(
            "SELECT channel, COUNT(*) AS count FROM delivery_attempts "
            "WHERE reminder_id = ? GROUP BY channel ORDER BY channel",
            (self.reminder.id,),
        ).fetchall()
        self.assertEqual(
            [(row["channel"], row["count"]) for row in attempts],
            [("alice", 1), ("home_assistant", 1), ("telegram", 2)],
        )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT status, error_code FROM delivery_attempts "
                "WHERE reminder_id = ? AND channel = 'telegram' ORDER BY attempt_number DESC LIMIT 1",
                (self.reminder.id,),
            ).fetchone()["status"],
            "succeeded",
        )
        self.assertIsNone(
            self.database.connection.execute(
                "SELECT error_code FROM delivery_attempts "
                "WHERE reminder_id = ? AND channel = 'telegram' ORDER BY attempt_number DESC LIMIT 1",
                (self.reminder.id,),
            ).fetchone()["error_code"]
        )
        self.assertEqual(
            self.repository.get_outbox(
                str(self.database.connection.execute(
                    "SELECT id FROM outbox WHERE reminder_id = ?", (self.reminder.id,)
                ).fetchone()["id"])
            ).payload["delivery_terminal_channels"],
            {"home_assistant": "ha_mobile_not_configured"},
        )

    async def test_manual_retry_clears_terminal_channel_state_and_starts_new_cycle(self):
        telegram = FakeTransport("telegram", DeliveryResult.success())
        settings = lambda: asyncio.sleep(
            0,
            result=ReminderSettings(spoken_endpoint="jarvis", phone_channels=("telegram",)),
        )
        scheduler = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram,
            mobile_transport=None,
            spoken_transport=None,
            default_chat_id=1,
            settings_provider=settings,
            interval_seconds=5,
            jitter_bound_seconds=0,
            now_fn=lambda: "2026-08-27T00:00:00Z",
        )
        await scheduler.run_once("2026-08-27T00:00:00Z")
        failed = self.repository.get_reminder(self.reminder.id)
        outbox_id = str(self.database.connection.execute(
            "SELECT id FROM outbox WHERE reminder_id = ?", (self.reminder.id,)
        ).fetchone()["id"])
        self.assertEqual(failed.delivery_state, "failed")
        self.assertIn("delivery_terminal_channels", self.repository.get_outbox(outbox_id).payload)

        retried = self.repository.manual_retry_reminder(
            self.reminder.id,
            expected_version=failed.version,
            context=CONTEXT,
            now="2026-08-27T00:01:00Z",
        )
        self.assertEqual(retried.delivery_state, "queued")
        retry_payload = self.repository.get_outbox(outbox_id).payload
        self.assertNotIn("delivery_policy", retry_payload)
        self.assertNotIn("delivery_terminal_channels", retry_payload)

        telegram_after_manual_retry = FakeTransport("telegram", DeliveryResult.success())
        second_scheduler = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram_after_manual_retry,
            mobile_transport=None,
            spoken_transport=None,
            default_chat_id=1,
            settings_provider=settings,
            interval_seconds=5,
            jitter_bound_seconds=0,
            now_fn=lambda: "2026-08-27T00:01:00Z",
        )
        await second_scheduler.run_once("2026-08-27T00:01:00Z")
        self.assertEqual(self.repository.get_reminder(self.reminder.id).delivery_state, "failed")
        self.assertEqual(telegram_after_manual_retry.calls, 0)
        counts = self.database.connection.execute(
            "SELECT channel, COUNT(*) AS count FROM delivery_attempts "
            "WHERE reminder_id = ? GROUP BY channel ORDER BY channel",
            (self.reminder.id,),
        ).fetchall()
        self.assertEqual([(row["channel"], row["count"]) for row in counts], [("jarvis", 2), ("telegram", 1)])
        cycles = self.database.connection.execute(
            "SELECT DISTINCT delivery_cycle_id FROM delivery_attempts WHERE reminder_id = ?",
            (self.reminder.id,),
        ).fetchall()
        self.assertEqual(len(cycles), 2)
