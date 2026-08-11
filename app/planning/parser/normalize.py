from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Normalize whitespace and Unicode without destroying user title text."""

    normalized = unicodedata.normalize("NFKC", value).replace("ё", "е").replace("Ё", "Е")
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def matching_text(value: str) -> str:
    """Return the deterministic, case-insensitive grammar representation."""

    return normalize_text(value).lower()


def normalize_for_idempotency(value: str) -> str:
    """Return a private stable command representation for HMAC derivation."""

    return matching_text(value)


def trim_title(value: str) -> str:
    """Clean punctuation left after removing command/date grammar spans."""

    value = _WHITESPACE_RE.sub(" ", value)
    value = value.strip(" \t\r\n,.;:!?—–-«»()[]{}")
    value = re.sub(r"^(?:,|и|а)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+(?:,|\.|;|:|!|\?)\s*", " ", value)
    return value.strip(" \t\r\n,.;:!?—–-«»()[]{}")
