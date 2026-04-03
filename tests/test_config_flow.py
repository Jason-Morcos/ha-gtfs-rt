import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "custom_components" / "gtfs_rt"


class FakeConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    def async_abort(self, reason):
        return {"type": "abort", "reason": reason}

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}


class FakeConfigEntryState:
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"


homeassistant = types.ModuleType("homeassistant")
config_entries = types.ModuleType("homeassistant.config_entries")
config_entries.ConfigFlow = FakeConfigFlow
config_entries.ConfigEntryState = FakeConfigEntryState
const = types.ModuleType("homeassistant.const")
const.CONF_NAME = "name"
homeassistant.config_entries = config_entries
homeassistant.const = const

sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.config_entries"] = config_entries
sys.modules["homeassistant.const"] = const

package = types.ModuleType("custom_components.gtfs_rt")
package.__path__ = [str(PACKAGE_ROOT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.gtfs_rt"] = package

config_module = types.ModuleType("custom_components.gtfs_rt.config")
config_module.normalize_feed_config = lambda data: data
sys.modules["custom_components.gtfs_rt.config"] = config_module

const_module = types.ModuleType("custom_components.gtfs_rt.const")
const_module.CONF_FEED_ID = "feed_id"
const_module.DOMAIN = "gtfs_rt"
sys.modules["custom_components.gtfs_rt.const"] = const_module

SPEC = importlib.util.spec_from_file_location(
    "custom_components.gtfs_rt.config_flow",
    PACKAGE_ROOT / "config_flow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeConfigEntriesManager:
    def __init__(self, entries):
        self._entries = entries
        self.updated = []
        self.reloaded = []

    def async_entries(self, _domain):
        return list(self._entries)

    def async_update_entry(self, entry, **kwargs):
        self.updated.append((entry, kwargs))

    def async_schedule_reload(self, entry_id):
        self.reloaded.append(entry_id)


class ConfigFlowImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_updates_not_loaded_entry_without_reloading(self):
        entry = types.SimpleNamespace(
            data={"feed_id": "feed-1", "name": "Old"},
            title="Old",
            entry_id="entry-1",
            state=FakeConfigEntryState.NOT_LOADED,
        )
        manager = FakeConfigEntriesManager([entry])
        flow = MODULE.GTFSRtConfigFlow()
        flow.hass = types.SimpleNamespace(config_entries=manager)

        result = await flow.async_step_import({"feed_id": "feed-1", "name": "New"})

        self.assertEqual(result, {"type": "abort", "reason": "already_configured"})
        self.assertEqual(len(manager.updated), 1)
        self.assertEqual(manager.reloaded, [])

    async def test_import_updates_loaded_entry_and_schedules_reload(self):
        entry = types.SimpleNamespace(
            data={"feed_id": "feed-1", "name": "Old"},
            title="Old",
            entry_id="entry-1",
            state=FakeConfigEntryState.LOADED,
        )
        manager = FakeConfigEntriesManager([entry])
        flow = MODULE.GTFSRtConfigFlow()
        flow.hass = types.SimpleNamespace(config_entries=manager)

        result = await flow.async_step_import({"feed_id": "feed-1", "name": "New"})

        self.assertEqual(result, {"type": "abort", "reason": "already_configured"})
        self.assertEqual(len(manager.updated), 1)
        self.assertEqual(manager.reloaded, ["entry-1"])


if __name__ == "__main__":
    unittest.main()
