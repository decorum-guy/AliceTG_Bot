from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from app.planning.errors import PlanningLocalTimeError
from app.planning.models import resolve_local_datetime, validate_timezone, validate_utc_timestamp
from app.planning.parser.normalize import matching_text


MONTHS: dict[str, int] = {
    "январь": 1,
    "января": 1,
    "февраль": 2,
    "февраля": 2,
    "март": 3,
    "марта": 3,
    "апрель": 4,
    "апреля": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июль": 7,
    "июля": 7,
    "август": 8,
    "августа": 8,
    "сентябрь": 9,
    "сентября": 9,
    "октябрь": 10,
    "октября": 10,
    "ноябрь": 11,
    "ноября": 11,
    "декабрь": 12,
    "декабря": 12,
}

WEEKDAYS: dict[str, int] = {
    "понедельник": 0,
    "понедельника": 0,
    "понедельнику": 0,
    "понедельником": 0,
    "понедельнике": 0,
    "вторник": 1,
    "вторника": 1,
    "вторнику": 1,
    "вторником": 1,
    "вторнике": 1,
    "среда": 2,
    "среду": 2,
    "среды": 2,
    "среде": 2,
    "средой": 2,
    "четверг": 3,
    "четверга": 3,
    "четвергу": 3,
    "четвергом": 3,
    "четверге": 3,
    "пятница": 4,
    "пятницу": 4,
    "пятницы": 4,
    "пятнице": 4,
    "пятницей": 4,
    "суббота": 5,
    "субботу": 5,
    "субботы": 5,
    "субботе": 5,
    "субботой": 5,
    "воскресенье": 6,
    "воскресенья": 6,
    "воскресенью": 6,
    "воскресеньем": 6,
    "воскресенье": 6,
}

NUMBER_WORDS: dict[str, int] = {
    "один": 1,
    "одна": 1,
    "одного": 1,
    "одной": 1,
    "одном": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "двумя": 2,
    "три": 3,
    "трех": 3,
    "тремя": 3,
    "четыре": 4,
    "четырех": 4,
    "пять": 5,
    "пяти": 5,
    "шесть": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
    "одиннадцать": 11,
    "одиннадцати": 11,
    "двенадцать": 12,
    "двенадцати": 12,
    "тринадцать": 13,
    "тринадцати": 13,
    "четырнадцать": 14,
    "четырнадцати": 14,
    "пятнадцать": 15,
    "пятнадцати": 15,
    "шестнадцать": 16,
    "шестнадцати": 16,
    "семнадцать": 17,
    "семнадцати": 17,
    "восемнадцать": 18,
    "восемнадцати": 18,
    "девятнадцать": 19,
    "девятнадцати": 19,
    "двадцать": 20,
    "двадцати": 20,
}


@dataclass(frozen=True)
class DateSpec:
    kind: Literal["anchored", "weekday", "explicit"]
    value: date
    span: tuple[int, int]
    label: str
    explicit_year: bool = False


@dataclass(frozen=True)
class RelativeSpec:
    seconds: int
    span: tuple[int, int]
    label: str


@dataclass(frozen=True)
class ClockSpec:
    minutes: int
    span: tuple[int, int]
    label: str
    explicit_period: bool
    word_form: bool

    @property
    def value(self) -> str:
        return f"{self.minutes // 60:02d}:{self.minutes % 60:02d}"


@dataclass(frozen=True)
class ClockRangeSpec:
    start: ClockSpec
    end: ClockSpec
    span: tuple[int, int]
    label: str


@dataclass(frozen=True)
class LocalTimeResult:
    utc: datetime | None
    error_code: str | None = None
    error_message: str | None = None


_ANCHOR_RE = re.compile(r"\b(?P<word>сегодня|завтра|послезавтра)\b", re.IGNORECASE)
_WEEKDAY_RE = re.compile(
    r"\b(?:(?:в|во|на)\s+)?(?P<word>понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b",
    re.IGNORECASE,
)
_MONTH_RE = re.compile(
    r"(?<!\d)(?P<day>[0-3]?\d)\s+(?P<month>январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)(?:\s+(?P<year>\d{4}))?(?!\d)",
    re.IGNORECASE,
)

_RELATIVE_RE = re.compile(
    r"\bчерез\s+(?:(?P<special>полтора\s+часа|пол\s*часа|минуту|час|день)|(?P<value>\d{1,4}|[а-яё]+)\s*(?P<unit>минут\w*|мин\w*|час\w*|дн\w*))\b",
    re.IGNORECASE,
)
_CLOCK_TOKEN = r"(?:\d{1,2}(?::\d{2})?|[а-яё]+)"
_RANGE_RE = re.compile(
    rf"\bс\s+(?P<start>{_CLOCK_TOKEN})\s+до\s+(?P<end>{_CLOCK_TOKEN})(?:\s+(?P<period>утра|дня|вечера|ночи))?\b",
    re.IGNORECASE,
)
_SINGLE_CLOCK_RE = re.compile(
    rf"\bв\s+(?P<clock>{_CLOCK_TOKEN})(?:\s+(?P<period>утра|дня|вечера|ночи))?(?:\s+час(?:а|ов)?)?\b",
    re.IGNORECASE,
)


def local_reference(reference_time_utc: str, timezone_name: str) -> datetime:
    validate_utc_timestamp(reference_time_utc, "reference_time_utc")
    validate_timezone(timezone_name, "timezone")
    instant = datetime.fromisoformat(reference_time_utc[:-1] + "+00:00")
    return instant.astimezone(ZoneInfo(timezone_name))


def parse_relative(text: str) -> RelativeSpec | None:
    match = _RELATIVE_RE.search(matching_text(text))
    if match is None:
        return None
    special = match.group("special")
    if special:
        special = special.replace("  ", " ")
        seconds = {
            "полтора часа": 90 * 60,
            "пол часа": 30 * 60,
            "полчаса": 30 * 60,
            "минуту": 60,
            "час": 60 * 60,
            "день": 24 * 60 * 60,
        }.get(special)
        if seconds is None:
            return None
        return RelativeSpec(seconds, match.span(), match.group(0))

    raw_value = match.group("value") or ""
    value = int(raw_value) if raw_value.isdigit() else NUMBER_WORDS.get(raw_value)
    unit = (match.group("unit") or "").lower()
    if value is None:
        return None
    if unit.startswith("мин"):
        seconds = value * 60
    elif unit.startswith("час"):
        seconds = value * 60 * 60
    else:
        seconds = value * 24 * 60 * 60
    if seconds <= 0 or seconds > 365 * 24 * 60 * 60:
        return None
    return RelativeSpec(seconds, match.span(), match.group(0))


def parse_date(text: str, reference_local: datetime) -> DateSpec | None:
    normalized = matching_text(text)
    matches: list[DateSpec] = []
    for match in _ANCHOR_RE.finditer(normalized):
        word = match.group("word")
        delta = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[word]
        matches.append(DateSpec("anchored", reference_local.date() + timedelta(days=delta), match.span(), word))

    for match in _WEEKDAY_RE.finditer(normalized):
        word = match.group("word")
        weekday = next((value for key, value in WEEKDAYS.items() if word.startswith(key[:4])), None)
        if weekday is None:
            continue
        delta = (weekday - reference_local.weekday()) % 7
        matches.append(
            DateSpec(
                "weekday",
                reference_local.date() + timedelta(days=delta),
                match.span(),
                word,
            )
        )

    for match in _MONTH_RE.finditer(normalized):
        month_word = match.group("month")
        month = next((value for key, value in MONTHS.items() if month_word.startswith(key[:4])), None)
        if month is None:
            continue
        day = int(match.group("day"))
        year_text = match.group("year")
        year = reference_local.year if year_text is None else int(year_text)
        try:
            parsed = date(year, month, day)
        except ValueError:
            raise ValueError("explicit date is impossible") from None
        matches.append(DateSpec("explicit", parsed, match.span(), match.group(0), year_text is not None))

    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError("more than one date was supplied")
    selected = matches[0]
    if selected.kind == "explicit" and not selected.explicit_year and selected.value < reference_local.date():
        try:
            selected = DateSpec(
                selected.kind,
                date(selected.value.year + 1, selected.value.month, selected.value.day),
                selected.span,
                selected.label,
                False,
            )
        except ValueError:
            raise ValueError("explicit date rollover is impossible") from None
    return selected


def _period_adjust(hour: int, period: str | None) -> int | None:
    if not 0 <= hour <= 23:
        return None
    if period is None:
        return hour
    if period == "утра":
        return hour if hour != 12 else 0
    if period == "ночи":
        return hour if hour in {0, 1, 2, 3, 4, 5} else None
    if period in {"дня", "вечера"}:
        if hour == 12:
            return 12
        return hour + 12 if 1 <= hour <= 11 else None
    return None


def _parse_clock_token(token: str, period: str | None, *, span: tuple[int, int], label: str) -> ClockSpec | None:
    lowered = matching_text(token)
    word_form = not lowered.isdigit() and not re.fullmatch(r"\d{1,2}:\d{2}", lowered)
    if lowered.isdigit() or re.fullmatch(r"\d{1,2}:\d{2}", lowered):
        if ":" in lowered:
            hour_text, minute_text = lowered.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        else:
            hour, minute = int(lowered), 0
    else:
        hour, minute = NUMBER_WORDS.get(lowered, -1), 0
    adjusted = _period_adjust(hour, period)
    if adjusted is None or minute < 0 or minute > 59:
        return None
    if word_form and period is None and 1 <= hour <= 12:
        # Bare inflected words such as «пять» do not establish AM/PM.
        return ClockSpec(adjusted * 60 + minute, span, label, False, True)
    return ClockSpec(adjusted * 60 + minute, span, label, period is not None, word_form)


def parse_clock_range(text: str) -> ClockRangeSpec | None:
    normalized = matching_text(text)
    match = _RANGE_RE.search(normalized)
    if match is None:
        return None
    period = match.group("period")
    start_token = match.group("start")
    end_token = match.group("end")
    start = _parse_clock_token(start_token, period, span=(match.start("start"), match.end("start")), label=start_token)
    end = _parse_clock_token(end_token, period, span=(match.start("end"), match.end("end")), label=end_token)
    if start is None or end is None:
        return None
    return ClockRangeSpec(start, end, match.span(), match.group(0))


def parse_single_clock(text: str) -> ClockSpec | None:
    normalized = matching_text(text)
    match = _SINGLE_CLOCK_RE.search(normalized)
    if match is None:
        return None
    token = match.group("clock")
    period = match.group("period")
    return _parse_clock_token(token, period, span=match.span(), label=match.group(0))


def local_datetime_to_utc(local_date: date, clock: ClockSpec, timezone_name: str) -> LocalTimeResult:
    try:
        return LocalTimeResult(
            resolve_local_datetime(
                local_date=local_date,
                local_time=clock.value,
                timezone_name=timezone_name,
                field="local time",
            )
        )
    except PlanningLocalTimeError as exc:
        if exc.code == "nonexistent_local_time":
            return LocalTimeResult(
                None,
                exc.code,
                "Указанное местное время не существует из-за перехода на летнее время.",
            )
        if exc.code == "ambiguous_local_time":
            return LocalTimeResult(
                None,
                exc.code,
                "Указанное местное время встречается дважды; уточните часовой пояс или смещение.",
            )
        raise


def local_day_bounds(local_date: date, timezone_name: str) -> tuple[str, str]:
    """Return the UTC range for one local day without using the machine clock."""

    midnight = ClockSpec(0, (0, 0), "00:00", True, False)
    next_midnight = ClockSpec(0, (0, 0), "00:00", True, False)
    start = local_datetime_to_utc(local_date, midnight, timezone_name)
    end = local_datetime_to_utc(local_date + timedelta(days=1), next_midnight, timezone_name)
    if start.utc is None or end.utc is None:
        raise ValueError(start.error_message or end.error_message or "local day has no valid UTC range")
    return (
        start.utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        end.utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def relative_due(reference_time_utc: str, seconds: int) -> datetime:
    validate_utc_timestamp(reference_time_utc, "reference_time_utc")
    if seconds <= 0:
        raise ValueError("relative duration must be positive")
    base = datetime.fromisoformat(reference_time_utc[:-1] + "+00:00")
    return base + timedelta(seconds=seconds)
