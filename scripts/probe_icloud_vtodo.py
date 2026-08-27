#!/usr/bin/env python3
"""Run the bounded, read-only Apple Reminders/VTODO capability probe.

The command reads the same server-only environment variables as the accepted
iCloud calendar provider. It never accepts a URL or credential as a command
line argument and prints only the probe's sanitized capability result.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.planning.providers.icloud import AiohttpCalDavTransport
from app.planning.providers.icloud_vtodo_probe import ICloudVTodoProbe


_CONFIG_NAMES = (
    "PLANNING_ICLOUD_ACCOUNT",
    "PLANNING_ICLOUD_PASSWORD",
    "PLANNING_ICLOUD_CALDAV_URL",
)


def _missing_configuration() -> list[str]:
    return [name for name in _CONFIG_NAMES if not os.getenv(name, "").strip()]


async def _run_live_probe(account: str, password: str, bootstrap_url: str) -> dict[str, object]:
    transport = AiohttpCalDavTransport(
        bootstrap_url=bootstrap_url,
        username=account,
        password=password,
        max_payload_bytes=2 * 1024 * 1024,
        max_redirects=3,
    )
    try:
        result = await ICloudVTodoProbe(
            transport=transport,
            account_name=account,
            max_collections=32,
            max_resources_per_collection=128,
        ).run()
        result["evidenceSource"] = "current_authorized_live_path"
        result["observedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result["liveEvidenceAvailable"] = True
        return result
    finally:
        await transport.close()


def main() -> int:
    missing = _missing_configuration()
    if missing:
        result = {
            "schemaVersion": "b4.apple-vtodo-probe.v2",
            "evidenceSource": "current_authorized_live_path",
            "liveEvidenceAvailable": False,
            "status": "not_configured",
            "missingConfigurationNames": missing,
            "note": "No live request was made; provide the existing server-side configuration.",
        }
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2

    try:
        result = asyncio.run(
            _run_live_probe(
                os.environ["PLANNING_ICLOUD_ACCOUNT"].strip(),
                os.environ["PLANNING_ICLOUD_PASSWORD"].strip(),
                os.environ["PLANNING_ICLOUD_CALDAV_URL"].strip(),
            )
        )
    except Exception:
        # The probe itself sanitizes provider failures. This final guard keeps
        # an unexpected local failure from printing exception text or secrets.
        result = {
            "schemaVersion": "b4.apple-vtodo-probe.v2",
            "evidenceSource": "current_authorized_live_path",
            "liveEvidenceAvailable": True,
            "status": "failed",
            "errors": [{"layer": "runner", "code": "provider_probe_failed"}],
        }
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
