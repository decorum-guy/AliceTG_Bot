from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.services.home_assistant import HomeAssistantClient


READY_HOLD_SECONDS = 7.0


def _get_float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default

    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got: {value!r}") from exc


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got: {value!r}") from exc


async def call_pushward(
    ha: HomeAssistantClient,
    service: str,
    payload: dict[str, object],
) -> None:
    await ha.call_service("pushward", service, payload)


async def main() -> None:
    ha_url = os.getenv("HA_URL", "http://homeassistant:8123").rstrip("/")
    token = os.getenv("HA_LONG_LIVED_TOKEN", "").strip()

    if not token:
        raise RuntimeError("HA_LONG_LIVED_TOKEN is required")

    off_hold_seconds = _get_float_env("PUSHWARD_COFFEE_OFF_HOLD_SECONDS", 3.0)
    ended_ttl = _get_int_env("PUSHWARD_COFFEE_ENDED_TTL_SECONDS", 2)

    slug = os.getenv("PUSHWARD_TEST_ACTIVITY_SLUG", "").strip()
    if not slug:
        slug = f"ha-coffee-machine-env-test-{int(time.time())}"

    ha = HomeAssistantClient(ha_url, token)

    try:
        print(
            "PushWard ENV off cleanup test: "
            f"slug={slug} "
            f"ready_hold_seconds={READY_HOLD_SECONDS} "
            f"off_hold_seconds={off_hold_seconds} "
            f"ended_ttl={ended_ttl}"
        )

        await call_pushward(
            ha,
            "create_activity",
            {
                "slug": slug,
                "name": "Кофемашина",
                "priority": 5,
                "stale_ttl": 300,
                "ended_ttl": ended_ttl,
            },
        )
        print("create_activity sent")

        await call_pushward(
            ha,
            "update_activity",
            {
                "slug": slug,
                "state": "ONGOING",
                "template": "generic",
                "state_text": "Кофемашина разогрета",
                "subtitle": "Работает 21 мин",
                "progress": 1.0,
                "icon": "cup.and.saucer",
                "accent_color": "#34C759",
            },
        )
        print(
            "ready update_activity sent; "
            f"waiting fixed {READY_HOLD_SECONDS} seconds before off state"
        )

        await asyncio.sleep(READY_HOLD_SECONDS)

        await call_pushward(
            ha,
            "update_activity",
            {
                "slug": slug,
                "state": "ONGOING",
                "template": "generic",
                "state_text": "Кофемашина выключена",
                "subtitle": " ",
                "progress": 0.0,
                "icon": "power",
                "accent_color": "#8E8E93",
            },
        )
        print(
            "off update_activity sent; "
            f"waiting {off_hold_seconds} seconds before end_activity"
        )

        await asyncio.sleep(off_hold_seconds)

        await call_pushward(
            ha,
            "end_activity",
            {
                "slug": slug,
                "completion_message": "Кофемашина выключена",
            },
        )
        print(f"end_activity sent; expected auto-delete after ended_ttl={ended_ttl} seconds")
        print("Watch iPhone: ready state -> off state -> end -> auto-delete.")

    finally:
        await ha.close()


if __name__ == "__main__":
    asyncio.run(main())
