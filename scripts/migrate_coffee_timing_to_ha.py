#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os

from app.services.app_state import AppStateStore
from app.services.coffee_timing_policy import (
    CoffeeTimingPolicyService,
    TimingPolicyError,
)
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError


async def _run(mode: str) -> int:
    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_LONG_LIVED_TOKEN", "").strip()
    state_path = os.environ.get("APP_STATE_PATH", "/app/data/state.json").strip()
    if not ha_url or not ha_token:
        print(json.dumps({"ok": False, "error": "missing_home_assistant_configuration"}))
        return 2

    ha = HomeAssistantClient(ha_url, ha_token)
    try:
        service = CoffeeTimingPolicyService(ha)
        try:
            if mode == "status":
                result = await service.migration_status()
            else:
                result = await service.migrate_legacy(
                    AppStateStore(state_path),
                    apply=mode == "apply",
                )
        except (HomeAssistantError, TimingPolicyError):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "mode": mode,
                        "error": "home_assistant_timing_helpers_unavailable",
                    }
                )
            )
            return 3
    finally:
        await ha.close()

    print(
        json.dumps(
            {
                "ok": True,
                "mode": mode,
                "status": result.status,
                "helpers": list(result.writes),
                "reason": result.reason,
                "warmup_duration_seconds": result.warmup_duration_seconds,
                "long_running_threshold_seconds": result.long_running_threshold_seconds,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy coffee timing values to canonical HA helpers",
    )
    parser.add_argument(
        "mode",
        choices=("dry-run", "apply", "status"),
        nargs="?",
        default="dry-run",
        help="status and dry-run are read-only; apply performs verified helper writes",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.mode)))


if __name__ == "__main__":
    main()
