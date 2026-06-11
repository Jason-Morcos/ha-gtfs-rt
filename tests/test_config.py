import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "custom_components" / "gtfs_rt"

voluptuous = types.ModuleType("voluptuous")
voluptuous.Optional = lambda key, default=None: key
voluptuous.Required = lambda key: key
voluptuous.Exclusive = lambda key, _group: key
sys.modules["voluptuous"] = voluptuous

homeassistant = types.ModuleType("homeassistant")
helpers_pkg = types.ModuleType("homeassistant.helpers")
config_validation = types.ModuleType("homeassistant.helpers.config_validation")
config_validation.string = str
sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.helpers"] = helpers_pkg
sys.modules["homeassistant.helpers.config_validation"] = config_validation

const_mod = types.ModuleType("homeassistant.const")
const_mod.CONF_NAME = "name"
const_mod.CONF_UNIQUE_ID = "unique_id"
sys.modules["homeassistant.const"] = const_mod

package = types.ModuleType("custom_components.gtfs_rt")
package.__path__ = [str(PACKAGE_ROOT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.gtfs_rt"] = package

const_spec = importlib.util.spec_from_file_location(
    "custom_components.gtfs_rt.const",
    PACKAGE_ROOT / "const.py",
)
const_module = importlib.util.module_from_spec(const_spec)
assert const_spec and const_spec.loader
sys.modules[const_spec.name] = const_module
const_spec.loader.exec_module(const_module)

config_spec = importlib.util.spec_from_file_location(
    "custom_components.gtfs_rt.config",
    PACKAGE_ROOT / "config.py",
)
config_module = importlib.util.module_from_spec(config_spec)
assert config_spec and config_spec.loader
sys.modules[config_spec.name] = config_module
config_spec.loader.exec_module(config_module)


class ConfigTests(unittest.TestCase):
    def test_normalize_feed_config_preserves_transit_app_fields(self):
        normalized = config_module.normalize_feed_config(
            {
                "name": "Example Transit",
                "entity_namespace": "gtfs_example",
                "trip_update_url": "https://example.com/tripupdates.pb",
                "transit_api_key": "test-key",
                "departures": [
                    {
                        "name": "Route 10",
                        "route": 100,
                        "stopid": 123,
                        "transit_global_stop_id": "AGENCY:123",
                        "transit_route": 10,
                        "unique_id": "route-10-stop-123",
                    }
                ],
            }
        )

        self.assertEqual(normalized["transit_api_key"], "test-key")
        self.assertEqual(
            normalized["departures"][0],
            {
                "name": "Route 10",
                "route": "100",
                "stopid": "123",
                "transit_global_stop_id": "AGENCY:123",
                "transit_route": "10",
                "unique_id": "route-10-stop-123",
            },
        )


if __name__ == "__main__":
    unittest.main()
