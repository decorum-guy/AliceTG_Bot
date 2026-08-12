from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from app.planning import MutationContext, PlanningDatabase, PlanningRepository
from app.planning.api.service import PlanningApiService
from app.planning.capabilities import planning_capability_metadata
from app.planning.errors import PlanningLocalTimeError, PlanningValidationError, PlanningVersionConflictError
from app.planning.events import EventService, propose_default_event_end
from app.planning.tasks import TaskService


CONTEXT = MutationContext(
    audience="operator",
    actor_id="a7-test",
    actor_type="operator",
    surface="operator",
).validate()
NOW = "2026-08-12T00:30:00Z"


class PlanningDomainServicesA7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "planning.sqlite3"
        self.database = PlanningDatabase(self.path)
        self.repository = PlanningRepository(self.database, now_fn=lambda: NOW)
        self.tasks = TaskService(self.database, repository=self.repository, now_fn=lambda: NOW)
        self.events = EventService(self.database, repository=self.repository, now_fn=lambda: NOW)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_date_only_task_round_trip_has_no_invented_instant(self) -> None:
        task = self.tasks.create(title="date-only", due_date="2026-08-12", context=CONTEXT)
        restored = self.tasks.get(task.id)
        self.assertEqual(restored.due_date, "2026-08-12")
        self.assertIsNone(restored.due_time)
        self.assertIsNone(restored.timezone)
        self.assertNotIn("00:00", restored.to_dict().values())

        with self.assertRaises(PlanningValidationError):
            self.tasks.create(
                title="date-only with fake timezone",
                due_date="2026-08-12",
                timezone="Europe/Moscow",
                context=CONTEXT,
            )

    def test_timed_tasks_support_moscow_and_berlin(self) -> None:
        moscow = self.tasks.create(
            title="Moscow",
            due_date="2026-08-12",
            due_time="10:30",
            timezone="Europe/Moscow",
            context=CONTEXT,
        )
        berlin = self.tasks.create(
            title="Berlin",
            due_date="2026-08-12",
            due_time="10:30",
            timezone="Europe/Berlin",
            context=CONTEXT,
        )
        self.assertEqual((moscow.due_time, moscow.timezone), ("10:30", "Europe/Moscow"))
        self.assertEqual((berlin.due_time, berlin.timezone), ("10:30", "Europe/Berlin"))

    def test_nonexistent_and_ambiguous_berlin_wall_times_are_rejected(self) -> None:
        with self.assertRaises(PlanningLocalTimeError) as nonexistent:
            self.tasks.create(
                title="DST gap",
                due_date="2026-03-29",
                due_time="02:30",
                timezone="Europe/Berlin",
                context=CONTEXT,
            )
        self.assertEqual(nonexistent.exception.code, "nonexistent_local_time")

        with self.assertRaises(PlanningLocalTimeError) as ambiguous:
            self.tasks.create(
                title="DST fold",
                due_date="2026-10-25",
                due_time="02:30",
                timezone="Europe/Berlin",
                context=CONTEXT,
            )
        self.assertEqual(ambiguous.exception.code, "ambiguous_local_time")

    def test_task_views_use_explicit_caller_timezone_and_reference_time(self) -> None:
        moscow_day = self.tasks.create(title="Moscow day", due_date="2026-08-12", context=CONTEXT)
        berlin_day = self.tasks.create(title="Berlin day", due_date="2026-08-11", context=CONTEXT)
        self.tasks.create(title="year rollover today", due_date="2027-01-01", context=CONTEXT)
        self.tasks.create(title="year rollover upcoming", due_date="2027-01-02", context=CONTEXT)

        moscow_today = self.tasks.today(
            reference_time_utc="2026-08-11T21:30:00Z",
            caller_timezone="Europe/Moscow",
        )
        berlin_today = self.tasks.today(
            reference_time_utc="2026-08-11T21:30:00Z",
            caller_timezone="Europe/Berlin",
        )
        self.assertEqual([item.id for item in moscow_today], [moscow_day.id])
        self.assertEqual([item.id for item in berlin_today], [berlin_day.id])

        rollover_today = self.tasks.today(
            reference_time_utc="2026-12-31T21:00:00Z",
            caller_timezone="Europe/Moscow",
        )
        self.assertEqual([item.title for item in rollover_today], ["year rollover today"])
        overdue = self.tasks.overdue(
            reference_time_utc="2027-01-01T00:00:00Z",
            caller_timezone="Europe/Moscow",
        )
        self.assertIn("Moscow day", [item.title for item in overdue])
        upcoming = self.tasks.upcoming(
            reference_time_utc="2026-12-31T21:00:00Z",
            caller_timezone="Europe/Moscow",
        )
        self.assertEqual([item.title for item in upcoming], ["year rollover upcoming"])

    def test_task_project_filter_priority_and_open_views(self) -> None:
        project = self.repository.create_project(name="A7 project", context=CONTEXT)
        selected = self.tasks.create(
            title="selected",
            due_date="2026-08-12",
            priority="high",
            project_id=project.id,
            context=CONTEXT,
        )
        completed = self.tasks.create(title="completed", due_date="2026-08-12", context=CONTEXT)
        archived = self.tasks.create(title="archived", due_date="2026-08-12", context=CONTEXT)
        self.tasks.complete(completed.id, expected_version=completed.version, context=CONTEXT)
        self.tasks.archive(archived.id, expected_version=archived.version, context=CONTEXT)
        visible = self.tasks.today(
            reference_time_utc=NOW,
            caller_timezone="Europe/Moscow",
            project_id=project.id,
        )
        self.assertEqual([item.id for item in visible], [selected.id])
        self.assertEqual(selected.priority, "high")
        with self.assertRaises(PlanningValidationError):
            self.tasks.create(title="bad priority", priority="urgent", context=CONTEXT)
        self.tasks.complete(selected.id, expected_version=selected.version, context=CONTEXT)
        with self.assertRaises(PlanningVersionConflictError):
            self.tasks.complete(selected.id, expected_version=selected.version, context=CONTEXT)
        archive_candidate = self.tasks.create(title="archive version", due_date="2026-08-12", context=CONTEXT)
        self.tasks.archive(archive_candidate.id, expected_version=archive_candidate.version, context=CONTEXT)
        with self.assertRaises(PlanningVersionConflictError):
            self.tasks.archive(archive_candidate.id, expected_version=archive_candidate.version, context=CONTEXT)

    def test_projects_are_deterministic_tombstones_without_cascade_or_default(self) -> None:
        first = self.repository.create_project(name="beta", context=CONTEXT)
        second = self.repository.create_project(name="Alpha", context=CONTEXT)
        task = self.tasks.create(title="retained reference", project_id=first.id, context=CONTEXT)
        self.assertEqual([item.name for item in self.repository.list_projects()], ["Alpha", "beta"])
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2)

        deleted = self.repository.delete_project(first.id, expected_version=first.version, context=CONTEXT)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual([item.id for item in self.repository.list_projects()], [second.id])
        self.assertEqual(self.repository.get_task(task.id).project_id, first.id)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_event_timed_and_all_day_shapes_update_and_local_only_state(self) -> None:
        timed = self.events.create(
            title="timed",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-12T21:30:00Z",
            end_at_utc="2026-08-12T22:30:00Z",
            context=CONTEXT,
        )
        all_day = self.events.create(
            title="multi-day",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-12",
            end_date_exclusive="2026-08-14",
            context=CONTEXT,
        )
        self.assertEqual((timed.sync_state, timed.provider_id, timed.provider_calendar_id), ("local_only", None, None))
        self.assertEqual((all_day.start_date, all_day.end_date_exclusive), ("2026-08-12", "2026-08-14"))
        updated = self.events.update(
            timed.id,
            expected_version=timed.version,
            title="updated timed",
            end_at_utc="2026-08-12T23:00:00Z",
            context=CONTEXT,
        )
        self.assertEqual(updated.title, "updated timed")
        self.assertEqual(updated.end_at_utc, "2026-08-12T23:00:00Z")
        with self.assertRaises(PlanningValidationError):
            self.events.create(
                title="mixed",
                all_day=True,
                timezone="Europe/Moscow",
                start_date="2026-08-12",
                end_date_exclusive="2026-08-13",
                start_at_utc="2026-08-12T10:00:00Z",
                end_at_utc="2026-08-12T11:00:00Z",
                context=CONTEXT,
            )
        with self.assertRaises(PlanningValidationError):
            self.events.create(
                title="recurrence",
                all_day=True,
                timezone="Europe/Moscow",
                start_date="2026-08-12",
                end_date_exclusive="2026-08-13",
                recurrence_rule="FREQ=DAILY",
                context=CONTEXT,
            )

    def test_event_local_day_queries_use_moscow_and_berlin_boundaries(self) -> None:
        all_day = self.events.create(
            title="all-day",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-13",
            end_date_exclusive="2026-08-15",
            context=CONTEXT,
        )
        crossing = self.events.create(
            title="crosses Moscow midnight",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-12T21:30:00Z",
            end_at_utc="2026-08-12T22:30:00Z",
            context=CONTEXT,
        )
        self.assertEqual(
            [item.title for item in self.events.tomorrow(
                reference_time_utc="2026-08-12T12:00:00Z", caller_timezone="Europe/Moscow"
            )],
            [all_day.title, crossing.title],
        )
        self.assertEqual(
            [item.title for item in self.events.today(
                reference_time_utc="2026-08-12T22:30:00Z", caller_timezone="Europe/Berlin"
            )],
            [all_day.title, crossing.title],
        )
        berlin_dst = self.events.create(
            title="Berlin DST day",
            all_day=True,
            timezone="Europe/Berlin",
            start_date="2026-03-29",
            end_date_exclusive="2026-03-30",
            context=CONTEXT,
        )
        self.assertIn(
            berlin_dst.id,
            [item.id for item in self.events.today(
                reference_time_utc="2026-03-29T12:00:00Z", caller_timezone="Europe/Berlin"
            )],
        )

    def test_event_range_returns_all_overlap_shapes_and_respects_exclusive_boundary(self) -> None:
        cases = [
            ("starts before", "2026-08-10T09:00:00Z", "2026-08-10T11:00:00Z"),
            ("ends after", "2026-08-10T11:00:00Z", "2026-08-10T13:00:00Z"),
            ("spans", "2026-08-10T08:00:00Z", "2026-08-10T13:00:00Z"),
            ("at start boundary", "2026-08-10T08:00:00Z", "2026-08-10T10:00:00Z"),
            ("at end boundary", "2026-08-10T12:00:00Z", "2026-08-10T14:00:00Z"),
        ]
        created = [
            self.events.create(
                title=title,
                all_day=False,
                timezone="Europe/Moscow",
                start_at_utc=start,
                end_at_utc=end,
                context=CONTEXT,
            )
            for title, start, end in cases
        ]
        result = self.events.query_range(
            from_utc="2026-08-10T10:00:00Z",
            to_utc="2026-08-10T12:00:00Z",
            caller_timezone="Europe/Moscow",
        )
        self.assertEqual([item.title for item in result], ["spans", "starts before", "ends after"])
        self.assertEqual(len({item.id for item in result}), 3)
        self.assertEqual(sorted(item.id for item in result), sorted(item.id for item in created[:3]))

        same_start_a = self.events.create(
            title="same-start-a",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-10T10:30:00Z",
            end_at_utc="2026-08-10T11:00:00Z",
            context=CONTEXT,
        )
        same_start_b = self.events.create(
            title="same-start-b",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-10T10:30:00Z",
            end_at_utc="2026-08-10T11:30:00Z",
            context=CONTEXT,
        )
        ordered = self.events.query_range(
            from_utc="2026-08-10T10:00:00Z",
            to_utc="2026-08-10T12:00:00Z",
            caller_timezone="Europe/Moscow",
        )
        tie = [item for item in ordered if item.id in {same_start_a.id, same_start_b.id}]
        self.assertEqual([item.id for item in tie], sorted((same_start_a.id, same_start_b.id)))

    def test_all_day_range_is_exclusive_and_overlapping_events_are_valid(self) -> None:
        self.events.create(
            title="day one",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-10",
            end_date_exclusive="2026-08-11",
            context=CONTEXT,
        )
        day_two = self.events.create(
            title="day two",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-11",
            end_date_exclusive="2026-08-12",
            context=CONTEXT,
        )
        self.events.create(
            title="overlap timed",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-10T12:00:00Z",
            end_at_utc="2026-08-10T13:00:00Z",
            context=CONTEXT,
        )
        result = self.events.query_range(
            from_utc="2026-08-11T00:00:00Z",
            to_utc="2026-08-12T00:00:00Z",
            caller_timezone="Europe/Moscow",
        )
        self.assertIn(day_two.id, [item.id for item in result])
        self.assertNotIn("day one", [item.title for item in result])

    def test_event_delete_and_provider_neutral_proposal_are_safe(self) -> None:
        event = self.events.create(
            title="delete me",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-12",
            end_date_exclusive="2026-08-13",
            context=CONTEXT,
        )
        deleted = self.events.delete(event.id, expected_version=event.version, context=CONTEXT)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(self.events.today(
            reference_time_utc=NOW, caller_timezone="Europe/Moscow"
        ), [])

        before = self.database.connection.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]
        proposal = propose_default_event_end(
            start_date="2026-08-12",
            start_time="23:30",
            timezone="Europe/Moscow",
        )
        after = self.database.connection.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]
        self.assertEqual(proposal.proposed_end_date, "2026-08-13")
        self.assertEqual(proposal.proposed_end_time, "00:30")
        self.assertEqual(after, before)
        with self.assertRaises(PlanningLocalTimeError):
            propose_default_event_end(
                start_date="2026-03-29",
                start_time="01:30",
                timezone="Europe/Berlin",
            )

    def test_capability_metadata_is_closed_truthful_and_status_content_free(self) -> None:
        metadata = planning_capability_metadata()
        self.assertTrue(dataclasses.is_dataclass(metadata))
        payload = metadata.to_dict()
        self.assertFalse(payload["events"]["recurrence"])
        self.assertFalse(payload["events"]["provider_sync"])
        self.assertTrue(payload["events"]["local_only"])
        self.assertNotIn("google", str(payload).lower())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metadata.tasks.read = False  # type: ignore[misc]

        service = PlanningApiService(self.database, now_fn=lambda: NOW)
        status = service.status(audience="operator", correlation_id="status-correlation")
        self.assertEqual(status["kind"], "status")
        self.assertEqual(status["capabilityMetadata"], payload)
        self.assertNotIn("items", status)
        self.assertNotIn("retained reference", str(status))

    def test_sqlite_integrity_remains_ok_after_domain_scenarios(self) -> None:
        self.tasks.create(title="integrity", due_date="2026-08-12", context=CONTEXT)
        self.events.create(
            title="integrity event",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-12",
            end_date_exclusive="2026-08-13",
            context=CONTEXT,
        )
        self.assertEqual(self.database.integrity_check(), "ok")


if __name__ == "__main__":
    unittest.main()
