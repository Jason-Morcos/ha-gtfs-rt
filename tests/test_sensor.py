import importlib.util
import sys
import types
import unittest
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "custom_components" / "gtfs_rt"

sys.modules.setdefault("requests", types.SimpleNamespace(get=None))

homeassistant = types.ModuleType("homeassistant")
sys.modules["homeassistant"] = homeassistant

util_pkg = types.ModuleType("homeassistant.util")


def throttle(_interval):
    def decorator(func):
        return func

    return decorator


util_pkg.Throttle = throttle
sys.modules["homeassistant.util"] = util_pkg

dt_mod = types.ModuleType("homeassistant.util.dt")
dt_mod.now = lambda: None
sys.modules["homeassistant.util.dt"] = dt_mod
util_pkg.dt = dt_mod

sensor_mod = types.ModuleType("homeassistant.components.sensor")


class PlatformSchema:
    def extend(self, _schema):
        return self


class SensorEntity:
    pass


sensor_mod.PLATFORM_SCHEMA = PlatformSchema()
sensor_mod.SensorEntity = SensorEntity
sys.modules["homeassistant.components"] = types.ModuleType("homeassistant.components")
sys.modules["homeassistant.components.sensor"] = sensor_mod

const_mod = types.ModuleType("homeassistant.const")
const_mod.ATTR_LATITUDE = "latitude"
const_mod.ATTR_LONGITUDE = "longitude"
const_mod.CONF_NAME = "name"
const_mod.CONF_UNIQUE_ID = "unique_id"
const_mod.UnitOfTime = types.SimpleNamespace(MINUTES="min")
sys.modules["homeassistant.const"] = const_mod

helpers_pkg = types.ModuleType("homeassistant.helpers")
sys.modules["homeassistant.helpers"] = helpers_pkg

device_registry_mod = types.ModuleType("homeassistant.helpers.device_registry")
device_registry_mod.DeviceEntryType = types.SimpleNamespace(SERVICE="service")
sys.modules["homeassistant.helpers.device_registry"] = device_registry_mod

entity_mod = types.ModuleType("homeassistant.helpers.entity")
entity_mod.DeviceInfo = dict
sys.modules["homeassistant.helpers.entity"] = entity_mod

package = types.ModuleType("custom_components.gtfs_rt")
package.__path__ = [str(PACKAGE_ROOT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.gtfs_rt"] = package

availability_mod = types.ModuleType("custom_components.gtfs_rt.availability")
availability_mod.should_mark_entity_unavailable = lambda **kwargs: False
sys.modules["custom_components.gtfs_rt.availability"] = availability_mod

config_mod = types.ModuleType("custom_components.gtfs_rt.config")
config_mod.FEED_CONFIG_SCHEMA = {}
config_mod.normalize_feed_config = lambda data: data
sys.modules["custom_components.gtfs_rt.config"] = config_mod

const_spec = importlib.util.spec_from_file_location(
    "custom_components.gtfs_rt.const",
    PACKAGE_ROOT / "const.py",
)
const_module = importlib.util.module_from_spec(const_spec)
assert const_spec and const_spec.loader
sys.modules[const_spec.name] = const_module
const_spec.loader.exec_module(const_module)

health_mod = types.ModuleType("custom_components.gtfs_rt.health")
health_mod.STATUS_LOOKUP_FAILED = "schedule_lookup_failed"
health_mod.STATUS_SERVICE_EXPECTED = "service_expected"
health_mod.StaticScheduleValidator = object
sys.modules["custom_components.gtfs_rt.health"] = health_mod

realtime_spec = importlib.util.spec_from_file_location(
    "custom_components.gtfs_rt.realtime",
    PACKAGE_ROOT / "realtime.py",
)
realtime_module = importlib.util.module_from_spec(realtime_spec)
assert realtime_spec and realtime_spec.loader
sys.modules[realtime_spec.name] = realtime_module
realtime_spec.loader.exec_module(realtime_module)

sensor_spec = importlib.util.spec_from_file_location(
    "custom_components.gtfs_rt.sensor",
    PACKAGE_ROOT / "sensor.py",
)
sensor_module = importlib.util.module_from_spec(sensor_spec)
assert sensor_spec and sensor_spec.loader
sys.modules[sensor_spec.name] = sensor_module
sensor_spec.loader.exec_module(sensor_module)

PublicTransportData = sensor_module.PublicTransportData
StopDetails = realtime_module.StopDetails


class SensorUpdateTests(unittest.TestCase):
    def test_stop_arrivals_failure_falls_back_to_trip_updates(self):
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("100214", "1234")],
            static_schedule_url=None,
            stop_arrivals_url_template="https://example.com/{stop_id}",
        )
        calls = []

        def fail_stop_arrivals():
            data.last_trip_update_error = "Stop-level arrivals unavailable: boom"
            return data.last_trip_update_error

        def fallback_trip_updates(_positions, _vehicles_trips, _occupancy):
            calls.append("fallback")
            data.info = {"100214": {"1234": ["departure"]}}

        data._update_stop_arrival_statuses = fail_stop_arrivals
        data._update_route_statuses = fallback_trip_updates

        data.update()

        self.assertEqual(calls, ["fallback"])
        self.assertIsNone(data.last_trip_update_error)
        self.assertEqual(data.info, {"100214": {"1234": ["departure"]}})

    def test_rate_limited_stop_arrivals_reuse_cached_data(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("100214", "1234")],
            static_schedule_url=None,
            stop_arrivals_url_template="https://example.com/{stop_id}",
        )
        future_departure = StopDetails(now + dt.timedelta(minutes=4), None, None, None)
        past_departure = StopDetails(now - dt.timedelta(minutes=1), None, None, None)
        data._last_stop_arrival_info = {"100214": {"1234": [past_departure, future_departure]}}
        fallback_calls = []

        class FakeResponse:
            status_code = 429
            headers = {"Retry-After": "120"}

            def raise_for_status(self):
                raise AssertionError("raise_for_status should not be called for 429 handling")

        sensor_module.requests.get = lambda *args, **kwargs: FakeResponse()
        data._update_route_statuses = lambda *_args: fallback_calls.append("fallback")

        data.update()

        self.assertEqual(fallback_calls, [])
        self.assertEqual(data.info, {"100214": {"1234": [future_departure]}})
        self.assertIsNone(data.last_trip_update_error)
        self.assertEqual(data._stop_arrivals_backoff_until, now + dt.timedelta(seconds=120))

    def test_rate_limit_backoff_skips_network_requests(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("100214", "1234")],
            static_schedule_url=None,
            stop_arrivals_url_template="https://example.com/{stop_id}",
        )
        future_departure = StopDetails(now + dt.timedelta(minutes=3), None, None, None)
        data._last_stop_arrival_info = {"100214": {"1234": [future_departure]}}
        data._stop_arrivals_backoff_until = now + dt.timedelta(minutes=2)
        fallback_calls = []

        def unexpected_get(*_args, **_kwargs):
            raise AssertionError("requests.get should not run during stop-arrivals backoff")

        sensor_module.requests.get = unexpected_get
        data._update_route_statuses = lambda *_args: fallback_calls.append("fallback")

        data.update()

        self.assertEqual(fallback_calls, [])
        self.assertEqual(data.info, {"100214": {"1234": [future_departure]}})
        self.assertIsNone(data.last_trip_update_error)

    def test_rate_limit_backoff_with_empty_cached_lists_falls_back_to_trip_updates(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("100214", "1234")],
            static_schedule_url=None,
            stop_arrivals_url_template="https://example.com/{stop_id}",
        )
        data._last_stop_arrival_info = {"100214": {"1234": []}}
        data._stop_arrivals_backoff_until = now + dt.timedelta(minutes=2)
        fallback_calls = []

        def unexpected_get(*_args, **_kwargs):
            raise AssertionError("requests.get should not run during stop-arrivals backoff")

        def fallback_trip_updates(_positions, _vehicles_trips, _occupancy):
            fallback_calls.append("fallback")
            data.info = {"100214": {"1234": ["departure"]}}

        sensor_module.requests.get = unexpected_get
        data._update_route_statuses = fallback_trip_updates

        data.update()

        self.assertEqual(fallback_calls, ["fallback"])
        self.assertEqual(data.info, {"100214": {"1234": ["departure"]}})
        self.assertIsNone(data.last_trip_update_error)


if __name__ == "__main__":
    unittest.main()
