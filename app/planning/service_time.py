"""Shared explicit-clock and caller-timezone helpers for Planning services."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import TypeAlias
from zoneinfo import ZoneInfo

from app.planning.errors import PlanningValidationError
from app.planning.models import resolve_local_datetime, validate_timezone, validate_utc_timestamp


UtcReference: TypeAlias = str | datetime


def as_utc_datetime(value: UtcReference, field: str = "reference_time_utc") -> datetime:
    """Return an aware UTC datetime from an injected, explicit reference clock."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise PlanningValidationError(f"{field} must be timezone-aware UTC")
        return value.astimezone(timezone.utc)
    validate_utc_timestamp(value, field)
    return datetime.fromisoformat(value[:-1] + "+00:00")


def utc_timestamp(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")


def caller_local_date(reference_time_utc: UtcReference, caller_timezone: str) -> date:
    validate_timezone(caller_timezone, "caller_timezone")
    return as_utc_datetime(reference_time_utc).astimezone(ZoneInfo(caller_timezone)).date()


def local_midnight_utc(local_date: date, caller_timezone: str) -> datetime:
    """Resolve a local calendar boundary without using machine-local time."""

    return resolve_local_datetime(
        local_date=local_date,
        local_time="00:00",
        timezone_name=caller_timezone,
        field="local midnight",
    )


def local_day_range_utc(
    local_start: date,
    local_end_exclusive: date,
    caller_timezone: str,
) -> tuple[str, str]:
    if local_end_exclusive <= local_start:
        raise PlanningValidationError("local end date must be later than local start date")
    validate_timezone(caller_timezone, "caller_timezone")
    return (
        utc_timestamp(local_midnight_utc(local_start, caller_timezone)),
        utc_timestamp(local_midnight_utc(local_end_exclusive, caller_timezone)),
    )


def local_date_window_for_utc(
    from_utc: UtcReference,
    to_utc: UtcReference,
    caller_timezone: str,
) -> tuple[date, date]:
    """Map a half-open UTC range to the local calendar-date range it touches."""

    validate_timezone(caller_timezone, "caller_timezone")
    start = as_utc_datetime(from_utc, "from_utc")
    end = as_utc_datetime(to_utc, "to_utc")
    if end <= start:
        raise PlanningValidationError("to_utc must be later than from_utc")
    local_end = end.astimezone(ZoneInfo(caller_timezone))
    end_date = local_end.date()
    if local_end.timetz().replace(tzinfo=None) != time.min:
        end_date += timedelta(days=1)
    start_date = start.astimezone(ZoneInfo(caller_timezone)).date()
    if end_date <= start_date:
        end_date = start_date + timedelta(days=1)
    return start_date, end_date
