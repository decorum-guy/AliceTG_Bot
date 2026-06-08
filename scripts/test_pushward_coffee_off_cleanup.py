from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.services.home_assistant import HomeAssistantClient


async def main() -> None:
    args = parse_args()
    ha_url = os.getenv("HA_URL", "http://homeassistant:8123").rstrip("/")
    token = os.getenv("HA_LONG_LIVED_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HA_LONG_LIVED_TOKEN is required")

    ha = HomeAssistantClient(ha_url, token)
    try:
        print(
            "PushWard off cleanup test: "
            f"slug={args.slug} ready_hold_seconds={args.ready_hold_seconds} "
            f"hold_seconds={args.hold_seconds} ended_ttl={args.ended_ttl}"
        )
        await call_pushward(
            ha,
            "create_activity",
            {
                "slug": args.slug,
                "name": "Кофемашина",
                "priority": 5,
                "stale_ttl": 300,
                "ended_ttl": args.ended_ttl,
            },
        )
        print("create_activity sent")

        await call_pushward(
            ha,
            "update_activity",
            {
                "slug": args.slug,
                "state": "ongoing",
                "template": "generic",
                "state_text": "Кофемашина разогрета",
                "subtitle": "Работает 21 мин",
                "progress": 1.0,
                "icon": "cup.and.saucer",
                "accent_color": "#34C759",
            },
        )
        print(f"ready state sent; waiting {args.ready_hold_seconds:g} seconds before off state")
        await asyncio.sleep(args.ready_hold_seconds)

        await call_pushward(
            ha,
            "update_activity",
            {
                "slug": args.slug,
                "state": "ongoing",
                "template": "generic",
                "state_text": "Кофемашина выключена",
                "subtitle": " ",
                "progress": 0.0,
                "icon": "power",
                "accent_color": "#8E8E93",
            },
        )
        print(f"off state sent; waiting {args.hold_seconds:g} seconds before end_activity")
        await asyncio.sleep(args.hold_seconds)

        await call_pushward(
            ha,
            "end_activity",
            {
                "slug": args.slug,
                "completion_message": "Кофемашина выключена",
            },
        )
        print(f"end_activity sent; expected auto-delete after {args.ended_ttl} seconds")

        if args.cleanup:
            await asyncio.sleep(args.ended_ttl + 2)
            await call_pushward(ha, "delete_activity", {"slug": args.slug})
            print("delete_activity cleanup sent")

        print("Watch iPhone: ready state -> off state -> end after hold_seconds -> removal after ended_ttl.")
    finally:
        await ha.close()


async def call_pushward(ha: HomeAssistantClient, service: str, payload: dict[str, object]) -> None:
    await ha.call_service("pushward", service, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test PushWard coffee off cleanup timing.")
    parser.add_argument("--slug", default="ha-coffee-machine-off-test")
    parser.add_argument("--ready-hold-seconds", type=float, default=7)
    parser.add_argument("--hold-seconds", type=float, default=5)
    parser.add_argument("--ended-ttl", type=int, default=3)
    parser.add_argument("--cleanup", action="store_true", help="Send best-effort delete_activity after end.")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main())
