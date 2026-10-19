"""Exercise UI flows in a subprocess so legacy HA stubs cannot leak between suites."""

import importlib
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock


def isolated_checks():
    import voluptuous as vol

    root = Path(__file__).resolve().parents[1]

    class Flow:
        def __init_subclass__(cls, **kwargs):
            pass

        def async_abort(self, reason):
            return {"type": "abort", "reason": reason}

        def async_create_entry(self, title, data):
            return {"type": "create_entry", "title": title, "data": data}

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_show_menu(self, **kwargs):
            return {"type": "menu", **kwargs}

        async def async_set_unique_id(self, unique_id):
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            pass

        def _get_reconfigure_entry(self):
            return self.entry

    ha = types.ModuleType("homeassistant")
    ce = types.ModuleType("homeassistant.config_entries")
    ce.ConfigFlow = ce.OptionsFlow = Flow
    ce.SOURCE_IMPORT = "import"
    ce.ConfigEntryState = types.SimpleNamespace(LOADED="loaded")
    ha.config_entries = ce
    hc = types.ModuleType("homeassistant.const")
    hc.CONF_NAME = "name"
    hc.CONF_UNIQUE_ID = "unique_id"
    core = types.ModuleType("homeassistant.core")
    core.callback = lambda function: function
    helpers = types.ModuleType("homeassistant.helpers")
    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = str
    selector = types.ModuleType("homeassistant.helpers.selector")
    selector.ObjectSelector = lambda: dict
    selector.SelectSelectorConfig = lambda **kwargs: kwargs
    selector.SelectSelector = lambda config: vol.In(
        [item["value"] for item in config["options"]]
    )
    helpers.selector = selector
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda hass: types.SimpleNamespace(
        async_get_entity_id=lambda *args: None
    )
    helpers.entity_registry = entity_registry
    for name, mod in {
        "homeassistant": ha,
        "homeassistant.config_entries": ce,
        "homeassistant.const": hc,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.config_validation": cv,
        "homeassistant.helpers.selector": selector,
    }.items():
        sys.modules[name] = mod
    pkg = types.ModuleType("custom_components.gtfs_rt")
    pkg.__path__ = [str(root / "custom_components/gtfs_rt")]
    sys.modules["custom_components"] = types.ModuleType("custom_components")
    sys.modules["custom_components.gtfs_rt"] = pkg
    feed = types.ModuleType("custom_components.gtfs_rt.feed")

    class FeedFetchError(Exception):
        pass

    class FeedAuthError(FeedFetchError):
        pass

    class InvalidFeedError(FeedFetchError):
        pass

    feed.FeedFetchError, feed.FeedAuthError, feed.InvalidFeedError = (
        FeedFetchError,
        FeedAuthError,
        InvalidFeedError,
    )
    feed.fetch_feed = Mock(return_value=object())
    sys.modules[feed.__name__] = feed
    module = importlib.import_module("custom_components.gtfs_rt.config_flow")
    normalize = importlib.import_module(
        "custom_components.gtfs_rt.config"
    ).normalize_feed_config

    class Manager:
        def __init__(self):
            self.entries, self.updates, self.reloads = [], [], []

        def async_entries(self, domain):
            return self.entries

        def async_update_entry(self, entry, **kwargs):
            self.updates.append(kwargs)
            for key, value in kwargs.items():
                setattr(entry, key, value)

        def async_schedule_reload(self, entry_id):
            self.reloads.append(entry_id)

    class Cases(unittest.IsolatedAsyncioTestCase):
        def setUp(self):
            self.manager = Manager()

            async def executor(fn, *args):
                return fn(*args)

            self.hass = types.SimpleNamespace(
                config_entries=self.manager, async_add_executor_job=executor
            )
            self.flow = module.GTFSRtConfigFlow()
            self.flow.hass = self.hass
            self.input = {
                "name": "Local",
                "trip_update_url": "https://example.org/trips",
            }
            feed.fetch_feed.reset_mock(side_effect=True)

        def entry(self, source="user"):
            data = normalize(
                {
                    **self.input,
                    "departures": [{"name": "Stop", "stopid": "S", "route": "R"}],
                    "entity_namespace": "stable",
                    "transit_api_key": "secret",
                    "headers": {"x-api-key": "private"},
                    "static_schedule_url": "https://example.org/static",
                }
            )
            entry = types.SimpleNamespace(
                data=data, title="Local", source=source, entry_id="entry"
            )
            self.manager.entries = [entry]
            self.flow.entry = entry
            return entry

        async def test_create_repeat_and_normalization(self):
            result = await self.flow.async_step_user(self.input)
            self.assertEqual(result["step_id"], "departure")
            result["data_schema"]({"name": "All", "stopid": "A"})
            result = await self.flow.async_step_departure(
                {"name": "All", "stopid": "A", "add_another": True}
            )
            self.assertEqual(result["step_id"], "departure")
            result = await self.flow.async_step_departure(
                {
                    "name": "Specific",
                    "stopid": "B",
                    "route": "R",
                    "unique_id": "preserved",
                }
            )
            self.assertEqual(result["type"], "create_entry")
            departures = result["data"]["departures"]
            self.assertEqual(departures[0]["route"], "")
            self.assertEqual(departures[1]["unique_id"], "preserved")
            self.assertTrue(departures[0]["unique_id"])

        async def test_form_validation_and_feed_errors(self):
            for patch, field, error in [
                (
                    {"trip_update_url": "file:///tmp/feed"},
                    "trip_update_url",
                    "invalid_url",
                ),
                ({"headers": {"secret": 12}}, "headers", "invalid_headers"),
                (
                    {"vehicle_position_url": "https:///broken"},
                    "vehicle_position_url",
                    "invalid_url",
                ),
            ]:
                result = await self.flow.async_step_user({**self.input, **patch})
                self.assertEqual(result["errors"][field], error)
            for exc, error in [
                (FeedAuthError, "invalid_auth"),
                (FeedFetchError, "cannot_connect"),
                (InvalidFeedError, "invalid_feed"),
            ]:
                feed.fetch_feed.side_effect = exc("sensitive URL")
                result = await self.flow.async_step_user(self.input)
                self.assertEqual(result["errors"], {"trip_update_url": error})
                self.assertNotIn("sensitive", str(result))

        async def test_whitespace_normalized_before_validation_and_identity(self):
            result = await self.flow.async_step_user(
                {
                    "name": " Local ",
                    "trip_update_url": " https://example.org/trips ",
                    "vehicle_position_url": "   ",
                }
            )
            self.assertEqual(result["step_id"], "departure")
            feed.fetch_feed.assert_called_with("https://example.org/trips", {})
            await self.flow.async_step_departure(
                {
                    "name": " First ",
                    "stopid": " S ",
                    "route": " R ",
                    "add_another": True,
                }
            )
            result = await self.flow.async_step_departure(
                {"name": "Second", "stopid": "S", "route": "R"}
            )
            self.assertEqual(result["errors"]["base"], "duplicate_departure")
            result = await self.flow.async_step_departure(
                {"name": " All ", "stopid": " B ", "route": "  "}
            )
            self.assertEqual(result["data"]["name"], "Local")
            self.assertEqual(result["data"]["departures"][0]["name"], "First")
            self.assertEqual(result["data"]["departures"][1]["stopid"], "B")
            self.assertEqual(result["data"]["departures"][1]["route"], "")

        async def test_optional_vehicle_url_is_validated(self):
            feed.fetch_feed.side_effect = [object(), InvalidFeedError("private")]
            result = await self.flow.async_step_user(
                {**self.input, "vehicle_position_url": "https://example.org/vehicles"}
            )
            self.assertEqual(result["errors"], {"vehicle_position_url": "invalid_feed"})
            self.assertEqual(feed.fetch_feed.call_count, 2)

        async def test_reconfigure_duplicate_url_does_not_update(self):
            self.entry()
            self.manager.entries.append(
                types.SimpleNamespace(
                    entry_id="other",
                    data={"trip_update_url": "https://example.org/other"},
                )
            )
            result = await self.flow.async_step_reconfigure(
                {"name": "New", "trip_update_url": "https://example.org/other"}
            )
            self.assertEqual(result["errors"]["base"], "already_configured")
            self.assertEqual(self.manager.updates, [])

        async def test_duplicates(self):
            self.entry()
            self.assertEqual(
                (await self.flow.async_step_user(self.input))["errors"]["base"],
                "already_configured",
            )
            self.manager.entries = []
            await self.flow.async_step_user(self.input)
            await self.flow.async_step_departure(
                {"name": "A", "stopid": "S", "route": "R", "add_another": True}
            )
            result = await self.flow.async_step_departure(
                {"name": "Different", "stopid": "S", "route": "R"}
            )
            self.assertEqual(result["errors"]["base"], "duplicate_departure")

        async def test_reconfigure_preserves_identity_and_submitted_fields(self):
            entry = self.entry()
            old = dict(entry.data)
            result = await self.flow.async_step_reconfigure()
            result["data_schema"](
                {"name": "New", "trip_update_url": "https://example.org/new"}
            )
            result = await self.flow.async_step_reconfigure(
                {
                    "name": "New",
                    "trip_update_url": "https://example.org/new",
                    "static_schedule_url": "",
                    "headers": old["headers"],
                    "transit_api_key": old["transit_api_key"],
                }
            )
            self.assertEqual(result["reason"], "reconfigure_successful")
            self.assertEqual(entry.data["feed_id"], old["feed_id"])
            self.assertEqual(entry.data["departures"], old["departures"])
            self.assertEqual(entry.data["transit_api_key"], "secret")
            self.assertEqual(entry.data["headers"], {"x-api-key": "private"})
            self.assertEqual(entry.data["entity_namespace"], "stable")
            self.assertNotIn("static_schedule_url", entry.data)
            self.assertEqual(self.manager.reloads, ["entry"])

        async def test_yaml_guards(self):
            entry = self.entry("import")
            self.assertEqual(
                (await self.flow.async_step_reconfigure())["reason"], "yaml_managed"
            )
            options = module.GTFSRtConfigFlow.async_get_options_flow(entry)
            options.hass = self.hass
            self.assertEqual(
                (await options.async_step_init())["reason"], "yaml_managed"
            )
            self.assertEqual(
                (await options.async_step_add_departure({}))["reason"], "yaml_managed"
            )
            self.assertEqual(
                (await options.async_step_remove_departure({}))["reason"],
                "yaml_managed",
            )
            self.assertEqual(self.manager.updates, [])

        async def test_options_add_remove_and_last_guard(self):
            entry = self.entry()
            original = entry.data["departures"][0]
            options = module.GTFSRtConfigFlow.async_get_options_flow(entry)
            options.hass = self.hass
            self.assertEqual(
                (await options.async_step_init())["menu_options"],
                ["add_departure", "remove_departure"],
            )
            result = await options.async_step_remove_departure(
                {"departure": original["unique_id"]}
            )
            self.assertEqual(result["errors"]["base"], "last_departure")
            result = await options.async_step_add_departure(
                {"name": "Duplicate", "stopid": "S", "route": "R"}
            )
            self.assertEqual(result["errors"]["base"], "duplicate_departure")
            result = await options.async_step_add_departure(
                {"name": "Other", "stopid": "B"}
            )
            self.assertEqual(result["type"], "create_entry")
            self.assertEqual(entry.data["departures"][0], original)
            result = await options.async_step_remove_departure(
                {"departure": entry.data["departures"][1]["unique_id"]}
            )
            self.assertEqual(result["type"], "create_entry")
            self.assertEqual(entry.data["departures"], [original])
            self.assertEqual(self.manager.reloads, ["entry", "entry"])

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Cases)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


class UIFlowIsolationTests(unittest.TestCase):
    def test_ui_flows(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--isolated"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if "--isolated" in sys.argv:
        raise SystemExit(0 if isolated_checks() else 1)
    unittest.main()
