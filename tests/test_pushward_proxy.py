from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.pushward import PushWardCoffeeActivity
from app.services.pushward_http import PushWardHttpClient, PushWardHttpError
from app.services.pushward_widgets import PushWardCoffeeWidget


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, bool]] = []
        self.error: PushWardHttpError | None = None

    async def request(self, method, path, payload=None, *, allow_not_found=False):
        self.calls.append((method, path, payload, allow_not_found))
        if self.error:
            raise self.error
        return None


class FakeHa:
    async def call_service(self, *args, **kwargs):
        raise AssertionError("Proxy mode must not call the Home Assistant PushWard service")


class PushWardProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            telegram_proxy_url="socks5://user:password@proxy.example:1080",
            pushward_integration_key="private-test-key",
            pushward_coffee_activity_enabled=True,
            pushward_coffee_activity_slug="ha-coffee-machine",
            pushward_coffee_ended_ttl_seconds=2,
            pushward_coffee_widget_enabled=True,
            pushward_coffee_widget_slug="ha-coffee-machine-widget",
            pushward_coffee_widget_name="Кофемашина",
            pushward_error_log_path=str(Path(self.temp.name) / "errors.log"),
        )
        self.state = SimpleNamespace(
            coffee_machine_state="on",
            coffee_on_since="2026-10-19T07:28:07+00:00",
            coffee_pushward_show_seconds=False,
        )
        self.timing = SimpleNamespace(warmup_duration_seconds=900, long_running_threshold_seconds=3600)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_activity_uses_direct_api_through_proxy(self) -> None:
        activity = PushWardCoffeeActivity(self.settings, FakeHa(), self.state, self.timing)
        api = FakeApi()
        activity._api = api

        self.assertTrue(await activity._create_activity())
        await activity._update_ready()
        await activity._end_activity()
        self.assertTrue(await activity._call("delete_activity", {"slug": "ha-coffee-machine"}))

        self.assertEqual(api.calls[0][0:2], ("POST", "/activities"))
        self.assertEqual(api.calls[0][2]["slug"], "ha-coffee-machine")
        self.assertEqual(api.calls[1][0:2], ("PATCH", "/activities/ha-coffee-machine"))
        self.assertEqual(api.calls[1][2]["state"], "ongoing")
        self.assertEqual(api.calls[1][2]["content"]["template"], "generic")
        self.assertEqual(api.calls[1][2]["content"]["state"], "Кофемашина разогрета")
        self.assertEqual(api.calls[2][2], {
            "state": "ended", "content": {"completion_message": "Кофемашина выключена"},
        })
        self.assertEqual(api.calls[3], ("DELETE", "/activities/ha-coffee-machine", None, True))

    async def test_missing_activity_is_recreated_and_transport_errors_do_not_disable_cycle(self) -> None:
        activity = PushWardCoffeeActivity(self.settings, FakeHa(), self.state, self.timing)
        api = FakeApi()
        activity._api = api
        activity._activity_active = True
        api.error = PushWardHttpError("PushWard API HTTP 404", status=404)

        await activity._update_ready()
        self.assertFalse(activity._activity_active)
        self.assertEqual(activity._consecutive_failures, 0)

        for _ in range(3):
            activity._record_failure("update_activity", PushWardHttpError("PushWard transport TimeoutError"))
        self.assertFalse(activity._degraded)
        self.assertEqual(activity._consecutive_failures, 3)

    async def test_widget_uses_same_proxy_api(self) -> None:
        widget = PushWardCoffeeWidget(self.settings, self.state, self.timing)
        api = FakeApi()
        widget._api = api
        await widget._patch_widget({"template": "stat_list"})
        self.assertEqual(api.calls, [
            ("PATCH", "/widgets/ha-coffee-machine-widget", {"content": {"template": "stat_list"}}, True),
        ])

    def test_socks_connector_is_used_without_exposing_proxy_credentials(self) -> None:
        with patch("app.services.pushward_http.ProxyConnector.from_url", return_value=object()) as connector:
            with patch("app.services.pushward_http.aiohttp.ClientSession") as session:
                client = PushWardHttpClient(self.settings)
                client._get_session()
        connector.assert_called_once_with(self.settings.telegram_proxy_url)
        self.assertEqual(session.call_args.kwargs["headers"], {"Authorization": "Bearer private-test-key"})
        self.assertIs(session.call_args.kwargs["connector"], connector.return_value)


if __name__ == "__main__":
    unittest.main()
