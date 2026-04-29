import asyncio
import csv
import json
import os
import signal
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import aiohttp


HA_URL = os.getenv("HA_URL", "http://127.0.0.1:8123")
HA_TOKEN = os.getenv("HA_TOKEN", "")

SNAPSHOT_EVERY_SECONDS = 5

JSONL_PATH = Path("/config/coffee_power_log.jsonl")
CSV_PATH = Path("/config/coffee_power_log.csv")

ENTITIES = {
    "switch.kofemashina": "switch",
    "sensor.kofemashina_potreblenie_toka": "current_a",
    "sensor.kofemashina_potrebliaemaia_moshchnost": "power_w",
    "sensor.kofemashina_tekushchee_napriazhenie": "voltage_v",
}

stop_event = asyncio.Event()
latest_states = {entity_id: None for entity_id in ENTITIES}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def make_ws_url(http_url):
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/api/websocket", "", "", ""))


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_state_value(entity_id):
    state = latest_states.get(entity_id)
    if not state:
        return None
    return state.get("state")


def build_row(event_type, changed_entity=None, old_state=None, new_state=None):
    switch_state = get_state_value("switch.kofemashina")
    current_a = to_float(get_state_value("sensor.kofemashina_potreblenie_toka"))
    power_w = to_float(get_state_value("sensor.kofemashina_potrebliaemaia_moshchnost"))
    voltage_v = to_float(get_state_value("sensor.kofemashina_tekushchee_napriazhenie"))

    return {
        "logged_at": now_iso(),
        "event_type": event_type,
        "changed_entity": changed_entity,
        "switch_state": switch_state,
        "current_a": current_a,
        "power_w": power_w,
        "voltage_v": voltage_v,
        "old_state": old_state,
        "new_state": new_state,
        "raw_states": {
            entity_id: latest_states.get(entity_id)
            for entity_id in ENTITIES
        },
    }


def append_logs(row):
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)

    with JSONL_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")

    csv_exists = CSV_PATH.exists()
    with CSV_PATH.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "logged_at",
                "event_type",
                "changed_entity",
                "switch_state",
                "current_a",
                "power_w",
                "voltage_v",
                "old_state",
                "new_state",
            ],
        )

        if not csv_exists:
            writer.writeheader()

        writer.writerow({
            "logged_at": row["logged_at"],
            "event_type": row["event_type"],
            "changed_entity": row["changed_entity"],
            "switch_state": row["switch_state"],
            "current_a": row["current_a"],
            "power_w": row["power_w"],
            "voltage_v": row["voltage_v"],
            "old_state": row["old_state"],
            "new_state": row["new_state"],
        })


def print_row(row):
    print(
        f'{row["logged_at"]} | {row["event_type"]} | '
        f'changed={row["changed_entity"]} | '
        f'switch={row["switch_state"]} | '
        f'current={row["current_a"]} A | '
        f'power={row["power_w"]} W | '
        f'voltage={row["voltage_v"]} V',
        flush=True,
    )


async def send_and_wait(ws, message_id, payload):
    await ws.send_json({"id": message_id, **payload})

    while True:
        msg = await ws.receive()

        if msg.type == aiohttp.WSMsgType.TEXT:
            data = json.loads(msg.data)

            if data.get("id") == message_id:
                return data

        if msg.type in (
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
            aiohttp.WSMsgType.CLOSE,
        ):
            raise RuntimeError("WebSocket connection closed while waiting for response")


async def load_initial_states(ws):
    response = await send_and_wait(ws, 2, {"type": "get_states"})

    if not response.get("success"):
        raise RuntimeError(f"Failed to get states: {response}")

    for state in response.get("result", []):
        entity_id = state.get("entity_id")
        if entity_id in ENTITIES:
            latest_states[entity_id] = state

    row = build_row("initial_snapshot")
    append_logs(row)
    print_row(row)


async def periodic_snapshots():
    while not stop_event.is_set():
        await asyncio.sleep(SNAPSHOT_EVERY_SECONDS)

        row = build_row("periodic_snapshot")
        append_logs(row)
        print_row(row)


async def listen():
    if not HA_TOKEN:
        raise RuntimeError("HA_TOKEN is empty. Create a long lived access token and pass it as HA_TOKEN.")

    ws_url = make_ws_url(HA_URL)

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            first = await ws.receive_json()

            if first.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected first WebSocket message: {first}")

            await ws.send_json({
                "type": "auth",
                "access_token": HA_TOKEN,
            })

            auth_response = await ws.receive_json()

            if auth_response.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant auth failed: {auth_response}")

            await load_initial_states(ws)

            subscribe_response = await send_and_wait(ws, 3, {
                "type": "subscribe_events",
                "event_type": "state_changed",
            })

            if not subscribe_response.get("success"):
                raise RuntimeError(f"Failed to subscribe: {subscribe_response}")

            print("Coffee power logger started. Press Ctrl+C to stop.", flush=True)

            snapshot_task = asyncio.create_task(periodic_snapshots())

            try:
                async for msg in ws:
                    if stop_event.is_set():
                        break

                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue

                    data = json.loads(msg.data)

                    if data.get("type") != "event":
                        continue

                    event = data.get("event", {})
                    event_data = event.get("data", {})
                    entity_id = event_data.get("entity_id")

                    if entity_id not in ENTITIES:
                        continue

                    old_state_obj = event_data.get("old_state")
                    new_state_obj = event_data.get("new_state")

                    latest_states[entity_id] = new_state_obj

                    old_value = old_state_obj.get("state") if old_state_obj else None
                    new_value = new_state_obj.get("state") if new_state_obj else None

                    row = build_row(
                        event_type="state_changed",
                        changed_entity=entity_id,
                        old_state=old_value,
                        new_state=new_value,
                    )

                    append_logs(row)
                    print_row(row)

            finally:
                snapshot_task.cancel()


def handle_stop():
    stop_event.set()


async def main():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_stop)

    await listen()


if __name__ == "__main__":
    asyncio.run(main())