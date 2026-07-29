#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os

from app.services.app_state import AppStateStore
from app.services.coffee_timing_policy import CoffeeTimingPolicyService
from app.services.home_assistant import HomeAssistantClient


async def _run(apply: bool) -> int:
    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_LONG_LIVED_TOKEN", "").strip()
    state_path = os.environ.get("APP_STATE_PATH", "/app/data/state.json").strip()
    if not ha_url or not ha_token:
        print(json.dumps({"ok": False, "error": "missing_home_assistant_configuration"}))
        return 2

    ha = HomeAssistantClient(ha_url, ha_token)
    try:
        service = CoffeeTimingPolicyService(ha)
        result = await service.migrate_legacy(
            AppStateStore(state_path),
            apply=apply,
        )
    finally:
        await ha.close()

    print(
        json.dumps(
            {
                "ok": True,
                "mode": "apply" if apply else "dry-run",
                "status": result.status,
                "helpers": list(result.writes),
                "reason": result.reason,
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
        "--apply",
        action="store_true",
        help="perform verified helper writes; the default is a read-only dry-run",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.apply)))


if __name__ == "__main__":
    main()
