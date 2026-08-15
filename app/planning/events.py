"""Provider-neutral local calendar-event domain service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.planning.errors import PlanningEventNotLocalOnlyError, PlanningValidationError
from app.planning.models import (
    CalendarEvent,
    MutationContext,
    utc_now,
    validate_date,
    validate_local_time,
    validate_timezone,
)
from app.planning.repositories import PlanningRepository
from app.planning.service_time import (
    UtcReference,
    as_utc_datetime,
    caller_local_date,
    local_date_window_for_utc,
    local_day_range_utc,
    resolve_local_datetime,
    utc_timestamp,
)


_UNSET = object()


def _repository(database: Any, repository: PlanningRepository | None, now_fn: Callable[[], str]) -> PlanningRepository:
    if repository is not None:
        return repository
    if isinstance(database, PlanningRepository):
        return database
    return PlanningRepository(database, now_fn=now_fn)


def _page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1001:
        raise PlanningValidationError("calendar_event.list limit is out of range")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
        raise PlanningValidationError("calendar_event.list offset is out of range")


def _event_sort_key(event: CalendarEvent, caller_timezone: str) -> tuple[int, str, str]:
    if event.all_day:
        return (0, event.start_date or "", event.id)
    assert event.start_at_utc is not None
    local_start = as_utc_datetime(event.start_at_utc, "calendar_event.start_at_utc").astimezone(
        ZoneInfo(caller_timezone)
    )
    return (1, local_start.isoformat(), event.id)


def is_native_local_only_event(event: CalendarEvent) -> bool:
    """Return whether an event is owned by native local Planning."""

    return (
        event.sync_state == "local_only"
        and event.provider_id is None
        and event.provider_calendar_id is None
    )


def require_native_local_only_event(event: CalendarEvent) -> None:
    """Fail closed unless the canonical event has native local ownership."""

    if not is_native_local_only_event(event):
        raise PlanningEventNotLocalOnlyError(
            "calendar event mutation requires a native local-only event"
        )


@dataclass(frozen=True)
class EventEndProposal:
    """A proposed, not persisted, one-hour event shape."""

    start_date: str
    start_time: str
    proposed_end_date: str
    proposed_end_time: str
    timezone: str
    start_at_utc: str
    proposed_end_at_utc: str
    duration_minutes: int = 60


def propose_default_event_end(
    *,
    start_date: str,
    start_time: str,
    timezone: str,
    duration_minutes: int = 60,
) -> EventEndProposal:
    """Propose a local end exactly 60 minutes later; never writes to SQLite."""

    if duration_minutes != 60:
        raise PlanningValidationError("the default event proposal duration is exactly 60 minutes")
    validate_date(start_date, "event.start_date")
    validate_local_time(start_time, "event.start_time")
    validate_timezone(timezone, "event.timezone")
    start_local_naive = datetime.combine(date.fromisoformat(start_date), time.fromisoformat(start_time))
    end_local_naive = start_local_naive + timedelta(minutes=duration_minutes)
    start_utc = resolve_local_datetime(
        local_date=start_date,
        local_time=start_time,
        timezone_name=timezone,
        field="event.start",
    )
    end_date = end_local_naive.date().isoformat()
    end_time = end_local_naive.time().isoformat(timespec="seconds" if start_time.count(":") == 2 else "minutes")
    end_utc = resolve_local_datetime(
        local_date=end_date,
        local_time=end_time,
        timezone_name=timezone,
        field="event.proposed_end",
    )
    return EventEndProposal(
        start_date=start_date,
        start_time=start_time,
        proposed_end_date=end_date,
        proposed_end_time=end_time,
        timezone=timezone,
        start_at_utc=utc_timestamp(start_utc),
        proposed_end_at_utc=utc_timestamp(end_utc),
    )


class EventService:
    """Canonical local event operations and caller-timezone range views."""

    def __init__(
        self,
        database: Any,
        *,
        repository: PlanningRepository | None = None,
        now_fn: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = _repository(database, repository, now_fn)

    def create(self, *, context: MutationContext, **fields: Any) -> CalendarEvent:
        allowed = {
            "title",
            "notes",
            "location",
            "all_day",
            "timezone",
            "start_at_utc",
            "end_at_utc",
            "start_date",
            "end_date_exclusive",
            "recurrence_rule",
            "source_ref",
            "sync_state",
            "provider_id",
            "provider_calendar_id",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise PlanningValidationError(f"event service received unknown fields: {sorted(unknown)}")
        if fields.get("sync_state", "local_only") != "local_only":
            raise PlanningValidationError("native Planning events must remain local_only")
        if fields.get("provider_id") is not None or fields.get("provider_calendar_id") is not None:
            raise PlanningValidationError("native Planning events cannot contain provider identity")
        fields.pop("sync_state", None)
        fields.pop("provider_id", None)
        fields.pop("provider_calendar_id", None)
        return self.repository.create_calendar_event(context=context, sync_state="local_only", **fields)

    def update(
        self,
        event_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        title: str | object = _UNSET,
        notes: str | None | object = _UNSET,
        location: str | None | object = _UNSET,
        all_day: bool | object = _UNSET,
        timezone: str | object = _UNSET,
        start_at_utc: str | None | object = _UNSET,
        end_at_utc: str | None | object = _UNSET,
        start_date: str | None | object = _UNSET,
        end_date_exclusive: str | None | object = _UNSET,
    ) -> CalendarEvent:
        require_native_local_only_event(self.get(event_id))
        fields = {
            name: value
            for name, value in {
                "title": title,
                "notes": notes,
                "location": location,
                "all_day": all_day,
                "timezone": timezone,
                "start_at_utc": start_at_utc,
                "end_at_utc": end_at_utc,
                "start_date": start_date,
                "end_date_exclusive": end_date_exclusive,
            }.items()
            if value is not _UNSET
        }
        return self.repository.update_calendar_event(
            event_id,
            expected_version=expected_version,
            context=context,
            sync_state="local_only",
            provider_id=None,
            provider_calendar_id=None,
            **fields,
        )

    def get(self, event_id: str) -> CalendarEvent:
        return self.repository.get_calendar_event(event_id)

    def delete(self, event_id: str, *, expected_version: int, context: MutationContext) -> CalendarEvent:
        require_native_local_only_event(self.get(event_id))
        return self.repository.delete_calendar_event(event_id, expected_version=expected_version, context=context)

    tombstone = delete

    def query_range(
        self,
        *,
        from_utc: UtcReference,
        to_utc: UtcReference,
        caller_timezone: str,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[CalendarEvent]:
        validate_timezone(caller_timezone, "caller_timezone")
        _page(limit, offset)
        start = as_utc_datetime(from_utc, "from_utc")
        end = as_utc_datetime(to_utc, "to_utc")
        if end <= start:
            raise PlanningValidationError("to_utc must be later than from_utc")
        local_start, local_end = local_date_window_for_utc(start, end, caller_timezone)
        all_events = self.repository.list_calendar_events(
            from_utc=utc_timestamp(start),
            to_utc=utc_timestamp(end),
            from_local_date=local_start.isoformat(),
            to_local_date=local_end.isoformat(),
            limit=1001,
            offset=0,
        )
        selected = []
        from_text = utc_timestamp(start)
        to_text = utc_timestamp(end)
        for event in all_events:
            if event.all_day:
                assert event.start_date is not None and event.end_date_exclusive is not None
                overlaps = event.start_date < local_end.isoformat() and event.end_date_exclusive > local_start.isoformat()
            else:
                assert event.start_at_utc is not None and event.end_at_utc is not None
                overlaps = event.start_at_utc < to_text and event.end_at_utc > from_text
            if overlaps:
                selected.append(event)
        selected.sort(key=lambda item: _event_sort_key(item, caller_timezone))
        return selected[offset : offset + limit]

    def query_local_day(
        self,
        *,
        local_date: str | date,
        caller_timezone: str,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[CalendarEvent]:
        selected_date = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
        from_utc, to_utc = local_day_range_utc(
            selected_date,
            selected_date + timedelta(days=1),
            caller_timezone,
        )
        return self.query_range(
            from_utc=from_utc,
            to_utc=to_utc,
            caller_timezone=caller_timezone,
            limit=limit,
            offset=offset,
        )

    def today(
        self,
        *,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[CalendarEvent]:
        return self.query_local_day(
            local_date=caller_local_date(reference_time_utc, caller_timezone),
            caller_timezone=caller_timezone,
            limit=limit,
            offset=offset,
        )

    def tomorrow(
        self,
        *,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[CalendarEvent]:
        return self.query_local_day(
            local_date=caller_local_date(reference_time_utc, caller_timezone) + timedelta(days=1),
            caller_timezone=caller_timezone,
            limit=limit,
            offset=offset,
        )

    def upcoming(
        self,
        *,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[CalendarEvent]:
        local_start = caller_local_date(reference_time_utc, caller_timezone) + timedelta(days=2)
        local_end = local_start + timedelta(days=365)
        from_utc, to_utc = local_day_range_utc(local_start, local_end, caller_timezone)
        return self.query_range(
            from_utc=from_utc,
            to_utc=to_utc,
            caller_timezone=caller_timezone,
            limit=limit,
            offset=offset,
        )
