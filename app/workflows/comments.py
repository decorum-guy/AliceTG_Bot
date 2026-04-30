from __future__ import annotations


NO_COMMENT = "-"


def normalize_order_comment(answer: str) -> str:
    normalized = answer.strip()
    if normalized.lower().startswith("нет"):
        return NO_COMMENT
    return normalized or NO_COMMENT
