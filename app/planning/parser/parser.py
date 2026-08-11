from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.planning.parser.dates import (
    DateSpec,
    ClockRangeSpec,
    ClockSpec,
    local_datetime_to_utc,
    local_day_bounds,
    local_reference,
    parse_clock_range,
    parse_date,
    parse_relative,
    parse_single_clock,
    relative_due,
)
from app.planning.parser.models import Ambiguity, Candidate, ParseResult, ParserInput
from app.planning.parser.normalize import matching_text, normalize_text, trim_title


_ALL_DAY_RE = re.compile(r"\b(?:весь день|целый день|на весь день)\b", re.IGNORECASE)
_NEXT_WEEK_RE = re.compile(r"\bна\s+следующ(?:ей|ую)\s+недел\w*\b", re.IGNORECASE)
# «вечера» is an explicit part-of-day suffix in «в пять вечера».  The
# unresolved preference is the standalone natural-language word «вечером».
_EVENING_RE = re.compile(r"\b(?:вечер|вечером|вечерний|вечернее|вечерние|вечернем)\b", re.IGNORECASE)
_RECURRENCE_RE = re.compile(
    r"\b(?:кажд\w*|ежеднев\w*|еженедельн\w*|ежемесячн\w*|по\s+(?:понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)|повторя\w*|повтор\w*)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_DOMAIN_RE = re.compile(r"\b(?:запиши|записать|добавь|добавить|создай|создать|поставь|поставить)\b", re.IGNORECASE)

_REMINDER_RE = re.compile(r"\b(?:напомни(?:те)?|напомнить|напоминани\w*)\b", re.IGNORECASE)
_TASK_RE = re.compile(
    r"\b(?:нужно|надо|необходимо|задач\w*|сделать|выполнить|поставь\s+задач\w*)\b",
    re.IGNORECASE,
)
_EVENT_RE = re.compile(
    r"\b(?:встреч\w*|встрет\w*|мероприят\w*|событи\w*|день\s+рождени\w*|календар\w*)\b",
    re.IGNORECASE,
)


def _utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date_string(value: date) -> str:
    return value.isoformat()


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    selected = sorted(spans)
    result: list[str] = []
    cursor = 0
    for start, end in selected:
        if start < cursor:
            continue
        result.append(text[cursor:start])
        result.append(" ")
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


def _domain(text: str) -> str | None:
    # A reminder marker owns the rest of the sentence.  Verbs such as
    # «сделать» may be ordinary reminder title text, not a second domain.
    if _REMINDER_RE.search(text):
        return "reminder"
    if _TASK_RE.search(text):
        # Keep explicit task language ahead of event nouns that may occur in
        # a title, for example «поставь задачу купить календарь».
        return "task"
    if _EVENT_RE.search(text):
        return "calendar_event"
    return None


def _query_candidate(text: str, request: ParserInput, reference: datetime) -> Candidate | None:
    text = matching_text(text)
    if re.search(r"\b(?:задач\w*|сделать|нужно сделать)\b", text) and re.search(
        r"\b(?:сегодня|на сегодня|сейчас)\b", text
    ) and re.search(
        r"\b(?:какие|какое|мои|список|есть|покажи|покаж\w*|что)\b", text
    ):
        today = reference.date().isoformat()
        return Candidate(
            "task",
            "query",
            {"query": "tasks_today", "date": today},
            f"Открытые задачи на {today}; отдельно посчитать просроченные.",
        )

    if re.search(r"\b(?:напоминани\w*)\b", text) and re.search(
        r"\b(?:какие|какое|мои|список|есть|покажи|покаж\w*)\b", text
    ):
        return Candidate(
            "reminder",
            "query",
            {"query": "active_reminders"},
            "Активные напоминания, сначала ближайшие.",
        )

    if re.search(r"\b(?:запланировано|план\w*|что\s+у\s+меня)\b", text) and re.search(
        r"\b(?:сегодня|завтра|послезавтра|понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*|\d{1,2}\s+[а-яё]+)\b",
        text,
    ):
        try:
            date_spec = parse_date(text, reference)
        except ValueError:
            return None
        if date_spec is None:
            return None
        try:
            from_utc, to_utc = local_day_bounds(date_spec.value, request.timezone)
        except ValueError:
            return None
        return Candidate(
            "calendar_event",
            "query",
            {
                "query": "events_day",
                "date": date_spec.value.isoformat(),
                "timezone": request.timezone,
                "from_utc": from_utc,
                "to_utc": to_utc,
            },
            f"События на {date_spec.value.isoformat()}, сначала события на весь день.",
        )
    return None


class PlanningParser:
    """A deterministic Russian grammar for the first Planning surfaces."""

    def parse(
        self,
        request: ParserInput | str,
        *,
        reference_time_utc: str | None = None,
        timezone_name: str | None = None,
        locale: str = "ru-RU",
    ) -> ParseResult:
        if isinstance(request, str):
            if reference_time_utc is None or timezone_name is None:
                raise ValueError("reference_time_utc and timezone are required")
            request = ParserInput(
                utterance=request,
                reference_time_utc=reference_time_utc,
                timezone=timezone_name,
                locale=locale,
            )
        if request.locale != "ru-RU":
            return self._error(request, "unsupported_locale", "Поддерживается только локаль ru-RU.")
        if not isinstance(request.utterance, str) or not normalize_text(request.utterance):
            return self._error(request, "empty_utterance", "Не удалось разобрать пустую фразу.")
        try:
            reference = local_reference(request.reference_time_utc, request.timezone)
        except (ValueError, TypeError) as exc:
            return self._error(request, "invalid_reference_context", str(exc))

        normalized = normalize_text(request.utterance)
        query = _query_candidate(normalized, request, reference)
        if query is not None:
            return ParseResult(query, "high", normalized_text=normalized)

        if _NEXT_WEEK_RE.search(normalized):
            return self._clarification(
                normalized,
                Ambiguity(
                    "date",
                    ("конкретный день", "конкретная дата"),
                    "Следующая неделя не задаёт однозначный день для записи.",
                ),
            )

        if _RECURRENCE_RE.search(normalized):
            return self._clarification(
                normalized,
                Ambiguity(
                    "recurrence",
                    ("одно событие",),
                    "Повторяющиеся записи отключены в Planning v1; повтор нужно убрать из фразы.",
                ),
            )

        if _EVENING_RE.search(normalized):
            return self._clarification(
                normalized,
                Ambiguity(
                    "time",
                    ("точное время",),
                    "Значение «вечером» ещё не настроено и не может быть угадано.",
                ),
            )

        domain = _domain(normalized)
        if domain is None:
            range_candidate = parse_clock_range(normalized)
            if range_candidate is not None and any(
                item.word_form and not item.explicit_period
                for item in (range_candidate.start, range_candidate.end)
            ):
                return self._clarification(
                    normalized,
                    Ambiguity("time", ("утро", "день", "вечер"), "Диапазон «с пяти до семи» не задаёт часть суток."),
                )
            if _AMBIGUOUS_DOMAIN_RE.search(normalized):
                return self._clarification(
                    normalized,
                    Ambiguity(
                        "domain",
                        ("задача", "напоминание", "событие"),
                        "Фраза не уточняет, в какой Planning-раздел её записать.",
                    ),
                )
            return ParseResult(
                None,
                "low",
                ambiguities=(
                    Ambiguity(
                        "domain",
                        ("задача", "напоминание", "событие"),
                        "Не найден однозначный тип записи.",
                    ),
                ),
                requires_confirmation=True,
                normalized_text=normalized,
            )

        try:
            relative = parse_relative(normalized)
            date_spec = parse_date(normalized, reference)
            clock_range = parse_clock_range(normalized)
            clock = parse_single_clock(normalized)
        except ValueError as exc:
            return self._clarification(
                normalized,
                Ambiguity("date", (), str(exc)),
            )

        if relative is not None and date_spec is not None:
            return self._clarification(
                normalized,
                Ambiguity("date", (), "В одной фразе указаны и относительная, и календарная дата."),
            )
        if clock_range is not None and clock is not None:
            # The single-clock matcher can see an «в 10» fragment in a larger
            # title.  A real range always wins when it owns the clock context.
            clock = None

        temporal_spans: list[tuple[int, int]] = []
        if relative is not None:
            temporal_spans.append(relative.span)
        if date_spec is not None:
            temporal_spans.append(date_spec.span)
        if clock_range is not None:
            temporal_spans.append(clock_range.span)
        elif clock is not None:
            temporal_spans.append(clock.span)
        all_day_match = _ALL_DAY_RE.search(normalized)
        if all_day_match is not None:
            temporal_spans.append(all_day_match.span())

        if domain == "reminder":
            if clock is not None and clock.word_form and not clock.explicit_period:
                return self._clarification(
                    normalized,
                    Ambiguity("time", ("утро", "день", "вечер"), "Словесное время без части суток неоднозначно."),
                )
            return self._parse_reminder(request, normalized, relative, date_spec, clock, clock_range, temporal_spans)
        if domain == "task":
            if clock is not None and clock.word_form and not clock.explicit_period:
                return self._clarification(
                    normalized,
                    Ambiguity("time", ("утро", "день", "вечер"), "Словесное время без части суток неоднозначно."),
                )
            return self._parse_task(request, normalized, reference, relative, date_spec, clock, temporal_spans)
        return self._parse_event(
            request,
            normalized,
            reference,
            relative,
            date_spec,
            clock,
            clock_range,
            all_day_match is not None,
            temporal_spans,
        )

    def _parse_reminder(
        self,
        request: ParserInput,
        normalized: str,
        relative: Any,
        date_spec: DateSpec | None,
        clock: ClockSpec | None,
        clock_range: ClockRangeSpec | None,
        temporal_spans: list[tuple[int, int]],
    ) -> ParseResult:
        if clock_range is not None:
            return self._clarification(
                normalized,
                Ambiguity(
                    "time",
                    ("точное время начала",),
                    "Напоминание требует одного точного момента, а не диапазона.",
                ),
            )
        if relative is None and (date_spec is None or clock is None):
            field = "time" if date_spec is not None else "date_and_time"
            reason = "Напоминание требует точного времени." if date_spec is not None else "Укажите дату и точное время напоминания."
            return self._clarification(normalized, Ambiguity(field, ("дата и время",), reason))

        if relative is not None:
            due_utc = relative_due(request.reference_time_utc, relative.seconds)
        else:
            assert date_spec is not None and clock is not None
            resolved = local_datetime_to_utc(date_spec.value, clock, request.timezone)
            if resolved.utc is None:
                return self._time_error(normalized, resolved.error_code, resolved.error_message)
            due_utc = resolved.utc
        reference_utc = datetime.fromisoformat(request.reference_time_utc[:-1] + "+00:00")
        if due_utc <= reference_utc:
            return self._clarification(
                normalized,
                Ambiguity("time", (), "Указанное время уже прошло; уточните будущую дату или время."),
            )
        title = self._title(normalized, temporal_spans, "reminder")
        if not title:
            return self._clarification(
                normalized,
                Ambiguity("title", (), "У напоминания должен быть текст."),
            )
        local_due = due_utc.astimezone(ZoneInfo(request.timezone))
        fields = {
            "title": title,
            "due_at_utc": _utc_string(due_utc),
            "timezone": request.timezone,
        }
        paraphrase = f"Напоминание «{title}» на {local_due.date().isoformat()} в {local_due:%H:%M} ({request.timezone})."
        return ParseResult(
            Candidate("reminder", "create", fields, paraphrase),
            "high",
            normalized_text=normalized,
        )

    def _parse_task(
        self,
        request: ParserInput,
        normalized: str,
        reference: datetime,
        relative: Any,
        date_spec: DateSpec | None,
        clock: ClockSpec | None,
        temporal_spans: list[tuple[int, int]],
    ) -> ParseResult:
        if relative is not None:
            due_utc = relative_due(request.reference_time_utc, relative.seconds)
            local_due = due_utc.astimezone(ZoneInfo(request.timezone))
            due_date = local_due.date()
            due_time = local_due.strftime("%H:%M")
            task_timezone: str | None = request.timezone
        elif date_spec is not None:
            due_date = date_spec.value
            if clock is None:
                if due_date < reference.date():
                    return self._clarification(
                        normalized,
                        Ambiguity("date", (), "Указанная дата уже прошла; уточните будущую дату."),
                    )
                due_time = None
                task_timezone = None
            else:
                resolved = local_datetime_to_utc(due_date, clock, request.timezone)
                if resolved.utc is None:
                    return self._time_error(normalized, resolved.error_code, resolved.error_message)
                if resolved.utc <= datetime.fromisoformat(request.reference_time_utc[:-1] + "+00:00"):
                    return self._clarification(
                        normalized,
                        Ambiguity("time", (), "Указанное время уже прошло; уточните будущую дату или время."),
                    )
                due_time = clock.value
                task_timezone = request.timezone
        else:
            return self._clarification(
                normalized,
                Ambiguity("date", ("сегодня", "завтра", "конкретная дата"), "У задачи должна быть дата."),
            )

        title = self._title(normalized, temporal_spans, "task")
        if not title:
            return self._clarification(normalized, Ambiguity("title", (), "У задачи должен быть текст."))
        fields = {
            "title": title,
            "due_date": _date_string(due_date),
            "due_time": due_time,
            "timezone": task_timezone,
            "priority": "none",
        }
        if due_time is None:
            paraphrase = f"Задача «{title}» на {due_date.isoformat()} без времени."
        else:
            paraphrase = f"Задача «{title}» на {due_date.isoformat()} в {due_time} ({request.timezone})."
        return ParseResult(Candidate("task", "create", fields, paraphrase), "high", normalized_text=normalized)

    def _parse_event(
        self,
        request: ParserInput,
        normalized: str,
        reference: datetime,
        relative: Any,
        date_spec: DateSpec | None,
        clock: ClockSpec | None,
        clock_range: ClockRangeSpec | None,
        all_day: bool,
        temporal_spans: list[tuple[int, int]],
    ) -> ParseResult:
        if date_spec is None and relative is None:
            return self._clarification(
                normalized,
                Ambiguity("date", ("конкретная дата",), "Событию нужна конкретная дата."),
            )
        if all_day and (clock is not None or clock_range is not None or relative is not None):
            return self._clarification(
                normalized,
                Ambiguity("representation", ("весь день", "точный интервал"), "Событие не может быть одновременно дневным и timed."),
            )

        title = self._title(normalized, temporal_spans, "calendar_event")
        if not title:
            return self._clarification(normalized, Ambiguity("title", (), "У события должен быть текст."))

        if all_day:
            assert date_spec is not None
            end_date = date_spec.value + timedelta(days=1)
            fields = {
                "title": title,
                "all_day": True,
                "timezone": request.timezone,
                "start_date": date_spec.value.isoformat(),
                "end_date_exclusive": end_date.isoformat(),
                "sync_state": "local_only",
            }
            paraphrase = f"Событие «{title}» на весь день {date_spec.value.isoformat()} ({request.timezone})."
            return ParseResult(Candidate("calendar_event", "create", fields, paraphrase), "high", normalized_text=normalized)

        if clock is not None and clock.word_form and not clock.explicit_period:
            return self._clarification(
                normalized,
                Ambiguity("time", ("утро", "день", "вечер"), "Словесное время без части суток неоднозначно."),
            )

        if clock_range is not None:
            if any(item.word_form and not item.explicit_period for item in (clock_range.start, clock_range.end)):
                return self._clarification(
                    normalized,
                    Ambiguity("time", ("утро", "день", "вечер"), "Диапазон «с пяти до семи» не задаёт часть суток."),
                )
            if date_spec is None:
                return self._clarification(
                    normalized,
                    Ambiguity("date", ("конкретная дата",), "Диапазону события нужна дата."),
                )
            start_result = local_datetime_to_utc(date_spec.value, clock_range.start, request.timezone)
            end_result = local_datetime_to_utc(date_spec.value, clock_range.end, request.timezone)
            if start_result.utc is None:
                return self._time_error(normalized, start_result.error_code, start_result.error_message)
            if end_result.utc is None:
                return self._time_error(normalized, end_result.error_code, end_result.error_message)
            start_utc, end_utc = start_result.utc, end_result.utc
            if end_utc <= start_utc:
                return self._clarification(
                    normalized,
                    Ambiguity("time", (), "Конец события должен быть позже начала; ночные диапазоны не угадываются."),
                )
            reference_utc = datetime.fromisoformat(request.reference_time_utc[:-1] + "+00:00")
            if start_utc <= reference_utc:
                return self._clarification(
                    normalized,
                    Ambiguity("time", (), "Указанное время уже прошло; уточните будущую дату или время."),
                )
            fields = {
                "title": title,
                "all_day": False,
                "timezone": request.timezone,
                "start_at_utc": _utc_string(start_utc),
                "end_at_utc": _utc_string(end_utc),
                "sync_state": "local_only",
            }
            paraphrase = f"Событие «{title}» {date_spec.value.isoformat()} с {clock_range.start.value} до {clock_range.end.value}."
            return ParseResult(Candidate("calendar_event", "create", fields, paraphrase), "high", normalized_text=normalized)

        if relative is not None:
            start_utc = relative_due(request.reference_time_utc, relative.seconds)
        else:
            assert date_spec is not None
            if clock is None:
                return self._clarification(
                    normalized,
                    Ambiguity("time", ("точное время начала",), "Для timed-события нужно указать время начала."),
                )
            resolved = local_datetime_to_utc(date_spec.value, clock, request.timezone)
            if resolved.utc is None:
                return self._time_error(normalized, resolved.error_code, resolved.error_message)
            start_utc = resolved.utc

        if start_utc <= datetime.fromisoformat(request.reference_time_utc[:-1] + "+00:00"):
            return self._clarification(
                normalized,
                Ambiguity("time", (), "Указанное время уже прошло; уточните будущую дату или время."),
            )

        proposed_end_utc = start_utc + timedelta(minutes=60)
        local_start = start_utc.astimezone(ZoneInfo(request.timezone))
        local_end = proposed_end_utc.astimezone(ZoneInfo(request.timezone))
        fields = {
            "title": title,
            "all_day": False,
            "timezone": request.timezone,
            "start_at_utc": _utc_string(start_utc),
            "proposed_end_at_utc": _utc_string(proposed_end_utc),
            "proposed_end_local": local_end.strftime("%H:%M"),
            "sync_state": "local_only",
        }
        ambiguity = Ambiguity(
            "end_time",
            (f"{local_start:%H:%M}–{local_end:%H:%M}",),
            "Для события без конца предложена длительность 60 минут; повторите полную фразу для записи.",
        )
        paraphrase = f"Предложить событие «{title}» {local_start.date().isoformat()} с {local_start:%H:%M} до {local_end:%H:%M}."
        return ParseResult(
            Candidate("calendar_event", "create", fields, paraphrase),
            "high",
            ambiguities=(ambiguity,),
            requires_confirmation=True,
            normalized_text=normalized,
        )

    @staticmethod
    def _title(text: str, spans: list[tuple[int, int]], domain: str) -> str:
        title = trim_title(_remove_spans(text, spans))
        if domain == "reminder":
            title = re.sub(
                r"^(?:пожалуйста\s+)?(?:напомни(?:те)?(?:\s+мне)?|напомнить(?:\s+мне)?|напоминани\w*)(?:\s+мне)?[\s,.:;-]*",
                "",
                title,
                flags=re.IGNORECASE,
            )
            title = re.sub(r"^что\s+", "", title, flags=re.IGNORECASE)
        elif domain == "task":
            title = re.sub(
                r"^(?:пожалуйста\s+)?(?:поставь\s+)?(?:задач\w*|нужно|надо|необходимо|сделать|выполнить)[\s,.:;-]*",
                "",
                title,
                flags=re.IGNORECASE,
            )
        else:
            title = re.sub(r"^(?:пожалуйста\s+)?(?:запланируй|создай\s+событие)[\s,.:;-]*", "", title, flags=re.IGNORECASE)
        return trim_title(title)

    @staticmethod
    def _clarification(normalized: str, ambiguity: Ambiguity) -> ParseResult:
        return ParseResult(
            None,
            "medium",
            ambiguities=(ambiguity,),
            requires_confirmation=True,
            normalized_text=normalized,
        )

    @staticmethod
    def _time_error(normalized: str, code: str | None, message: str | None) -> ParseResult:
        return ParseResult(
            None,
            "low",
            requires_confirmation=False,
            normalized_text=normalized,
            error_code=code or "invalid_local_time",
            error_message=message or "Указанное местное время нельзя использовать.",
        )

    @staticmethod
    def _error(request: ParserInput, code: str, message: str) -> ParseResult:
        return ParseResult(None, "low", normalized_text=matching_text(request.utterance), error_code=code, error_message=message)


def parse_planning_text(
    utterance: str,
    *,
    reference_time_utc: str,
    timezone_name: str,
    locale: str = "ru-RU",
) -> ParseResult:
    return PlanningParser().parse(
        ParserInput(
            utterance=utterance,
            reference_time_utc=reference_time_utc,
            timezone=timezone_name,
            locale=locale,
        )
    )
