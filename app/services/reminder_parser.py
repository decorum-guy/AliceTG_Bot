from __future__ import annotations

import re
from dataclasses import dataclass

MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 24 * 60 * 60

_NUMBER_DELAY_RE = re.compile(
    r"\bчерез\s+(?P<value>\d{1,3})\s*(?P<unit>минуту|минуты|минут|мин|час|часа|часов)\b",
    re.IGNORECASE,
)
_HOUR_DELAY_RE = re.compile(r"\bчерез\s+час\b", re.IGNORECASE)
_HALF_HOUR_DELAY_RE = re.compile(r"\bчерез\s+пол\s*часа\b|\bчерез\s+полчаса\b", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedReminder:
    text: str
    delay_seconds: int
    human_delay_text: str


def parse_reminder_request(text: str) -> ParsedReminder | None:
    raw_text = " ".join(text.strip().split())
    if not raw_text:
        return None

    match = _NUMBER_DELAY_RE.search(raw_text)
    if match:
        value = int(match.group("value"))
        unit = match.group("unit").lower()
        delay_seconds = value * 3600 if unit.startswith("час") else value * 60
        human_delay_text = f"{value} {_hour_word(value) if unit.startswith('час') else _minute_word(value)}"
        return _build_result(raw_text, delay_seconds, human_delay_text, match)

    match = _HOUR_DELAY_RE.search(raw_text)
    if match:
        return _build_result(raw_text, 3600, "1 час", match)

    match = _HALF_HOUR_DELAY_RE.search(raw_text)
    if match:
        return _build_result(raw_text, 30 * 60, "30 минут", match)

    return None


def parse_delay_only(text: str) -> tuple[int, str] | None:
    raw_text = " ".join(text.strip().split())
    if not raw_text:
        return None

    match = _NUMBER_DELAY_RE.search(raw_text)
    if match:
        value = int(match.group("value"))
        unit = match.group("unit").lower()
        delay_seconds = value * 3600 if unit.startswith("час") else value * 60
        if delay_seconds < MIN_DELAY_SECONDS or delay_seconds > MAX_DELAY_SECONDS:
            return None
        human_delay_text = f"{value} {_hour_word(value) if unit.startswith('час') else _minute_word(value)}"
        return delay_seconds, human_delay_text

    if _HOUR_DELAY_RE.search(raw_text):
        return 3600, "1 час"

    if _HALF_HOUR_DELAY_RE.search(raw_text):
        return 30 * 60, "30 минут"

    return None


def _build_result(raw_text: str, delay_seconds: int, human_delay_text: str, match: re.Match[str]) -> ParsedReminder | None:
    if delay_seconds < MIN_DELAY_SECONDS or delay_seconds > MAX_DELAY_SECONDS:
        return None

    reminder_text = _cleanup_reminder_text((raw_text[: match.start()] + raw_text[match.end() :]).strip())
    if not reminder_text:
        reminder_text = _cleanup_reminder_text(raw_text[: match.start()].strip())
    if not reminder_text:
        return None
    return ParsedReminder(text=reminder_text, delay_seconds=delay_seconds, human_delay_text=human_delay_text)


def _cleanup_reminder_text(text: str) -> str:
    cleaned = text.strip(" ,.!?;:")
    cleaned = re.sub(r"^(пожалуйста\s+)?(напомнить мне|напомни мне|напомнить|напомни)[\s,.:;-]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^что\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,.!?;:")
    return cleaned


def _minute_word(minutes: int) -> str:
    if 11 <= minutes % 100 <= 14:
        return "минут"
    if minutes % 10 == 1:
        return "минуту"
    if minutes % 10 in {2, 3, 4}:
        return "минуты"
    return "минут"


def _hour_word(hours: int) -> str:
    if 11 <= hours % 100 <= 14:
        return "часов"
    if hours % 10 == 1:
        return "час"
    if hours % 10 in {2, 3, 4}:
        return "часа"
    return "часов"
