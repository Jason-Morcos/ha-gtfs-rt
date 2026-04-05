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
PublicTransportSensor = sensor_module.PublicTransportSensor
StopDetails = realtime_module.StopDetails
RealtimePosition = realtime_module.RealtimePosition


class SensorUpdateTests(unittest.TestCase):
    def test_sensor_exposes_upcoming_departures_attribute(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        next_buses = [
            StopDetails(
                now + dt.timedelta(minutes=2),
                RealtimePosition(47.1, -122.1),
                "FEW_SEATS_AVAILABLE",
                120,
            ),
            StopDetails(now + dt.timedelta(minutes=9), None, "STANDING_ROOM_ONLY", 300),
            StopDetails(now + dt.timedelta(minutes=17), None, None, None),
            StopDetails(now + dt.timedelta(minutes=24), None, None, None),
            StopDetails(now + dt.timedelta(minutes=31), None, None, None),
            StopDetails(now + dt.timedelta(minutes=38), None, None, None),
        ]

        class FakeData:
            def __init__(self):
                self.info = {"100214": {"1234": next_buses}}
                self.last_trip_update_error = None

            def get_schedule_status(self, _route, _stop):
                return None

        sensor = PublicTransportSensor(
            data=FakeData(),
            stop="1234",
            route="100214",
            name="Route 372",
            unique_id="test-unique-id",
        )

        attrs = sensor.extra_state_attributes

        self.assertEqual(sensor.state, 2)
        self.assertEqual(attrs[sensor_module.ATTR_NEXT_UP], "16:09")
        self.assertEqual(len(attrs[sensor_module.ATTR_UPCOMING_DEPARTURES]), 5)
        self.assertEqual(
            attrs[sensor_module.ATTR_UPCOMING_DEPARTURES][0],
            {
                "due_at": "16:02",
                "due_in": 2,
                "delay_minutes": 2.0,
                "occupancy": "FEW_SEATS_AVAILABLE",
                "latitude": 47.1,
                "longitude": -122.1,
            },
        )
        self.assertEqual(
            attrs[sensor_module.ATTR_UPCOMING_DEPARTURES][1],
            {
                "due_at": "16:09",
                "due_in": 9,
                "delay_minutes": 5.0,
                "occupancy": "STANDING_ROOM_ONLY",
            },
        )

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

    def test_stop_arrivals_dedupes_requests_for_shared_stops(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("100214", "1234"), ("100225", "1234")],
            static_schedule_url=None,
            stop_arrivals_url_template="https://example.com/{stop_id}",
        )
        request_urls = []

        class FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 200,
                    "data": {
                        "entry": {
                            "arrivalsAndDepartures": [
                                {
                                    "routeId": "1_100214",
                                    "predictedDepartureTime": int(
                                        (now + dt.timedelta(minutes=5)).timestamp() * 1000
                                    ),
                                    "scheduledDepartureTime": int(
                                        (now + dt.timedelta(minutes=4)).timestamp() * 1000
                                    ),
                                },
                                {
                                    "routeId": "1_100225",
                                    "predictedDepartureTime": int(
                                        (now + dt.timedelta(minutes=8)).timestamp() * 1000
                                    ),
                                    "scheduledDepartureTime": int(
                                        (now + dt.timedelta(minutes=7)).timestamp() * 1000
                                    ),
                                },
                            ]
                        }
                    },
                }

        def fake_get(url, **_kwargs):
            request_urls.append(url)
            return FakeResponse()

        sensor_module.requests.get = fake_get

        data.update()

        self.assertEqual(request_urls, ["https://example.com/1234"])
        self.assertEqual(len(data.info["100214"]["1234"]), 1)
        self.assertEqual(len(data.info["100225"]["1234"]), 1)
        self.assertEqual(data.info["100214"]["1234"][0].arrival_time, now + dt.timedelta(minutes=5))
        self.assertEqual(data.info["100225"]["1234"][0].arrival_time, now + dt.timedelta(minutes=8))

    def test_bootstrapped_stop_arrivals_refresh_one_stop_per_update(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("100214", "1234"), ("102732", "5678")],
            static_schedule_url=None,
            stop_arrivals_url_template="https://example.com/{stop_id}",
        )
        departure_a = StopDetails(now + dt.timedelta(minutes=4), None, None, None)
        departure_b = StopDetails(now + dt.timedelta(minutes=7), None, None, None)
        data._last_stop_arrival_info = {
            "100214": {"1234": [departure_a]},
            "102732": {"5678": [departure_b]},
        }
        data._stop_arrivals_last_refresh = {
            "1234": now - dt.timedelta(minutes=2),
            "5678": now - dt.timedelta(minutes=2),
        }
        request_urls = []

        class FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {"code": 200, "data": {"entry": {"arrivalsAndDepartures": []}}}

        def fake_get(url, **_kwargs):
            request_urls.append(url)
            return FakeResponse()

        sensor_module.requests.get = fake_get

        data.update()
        data.update()

        self.assertEqual(
            request_urls,
            ["https://example.com/1234", "https://example.com/5678"],
        )
        self.assertEqual(data.info["102732"]["5678"], [])
        self.assertEqual(data._stop_arrivals_last_refresh["1234"], now)
        self.assertEqual(data._stop_arrivals_last_refresh["5678"], now)

    def test_rate_limit_keeps_successful_partial_stop_arrivals_cache(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("100214", "1234"), ("102732", "5678")],
            static_schedule_url=None,
            stop_arrivals_url_template="https://example.com/{stop_id}",
        )
        responses = []

        class SuccessResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 200,
                    "data": {
                        "entry": {
                            "arrivalsAndDepartures": [
                                {
                                    "routeId": "1_100214",
                                    "predictedDepartureTime": int(
                                        (now + dt.timedelta(minutes=6)).timestamp() * 1000
                                    ),
                                    "scheduledDepartureTime": int(
                                        (now + dt.timedelta(minutes=5)).timestamp() * 1000
                                    ),
                                }
                            ]
                        }
                    },
                }

        class RateLimitedResponse:
            status_code = 429
            headers = {"Retry-After": "120"}

            def raise_for_status(self):
                raise AssertionError("raise_for_status should not be called for 429 handling")

        def fake_get(url, **_kwargs):
            responses.append(url)
            if url.endswith("/1234"):
                return SuccessResponse()
            return RateLimitedResponse()

        sensor_module.requests.get = fake_get

        result = data._update_stop_arrival_statuses()

        self.assertIsNone(result)
        self.assertEqual(
            responses,
            ["https://example.com/1234", "https://example.com/5678"],
        )
        self.assertEqual(
            data.info["100214"]["1234"][0].arrival_time,
            now + dt.timedelta(minutes=6),
        )
        self.assertEqual(data.info["102732"]["5678"], [])
        self.assertEqual(data._stop_arrivals_backoff_until, now + dt.timedelta(seconds=120))


class SensorStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_setup_entry_does_not_force_update_before_add(self):
        hass = types.SimpleNamespace(data={})
        entry = types.SimpleNamespace(
            entry_id="entry-1",
            data={
                const_module.CONF_TRIP_UPDATE_URL: "https://example.com/tripupdates.pb",
                const_module.CONF_DEPARTURES: [
                    {
                        const_module.CONF_ROUTE: "100214",
                        const_module.CONF_STOP_ID: "1234",
                        const_mod.CONF_NAME: "Route 372",
                        const_mod.CONF_UNIQUE_ID: "test-unique-id",
                    }
                ],
            },
        )
        recorded = {}

        def async_add_entities(entities, update_before_add=False):
            recorded["entities"] = list(entities)
            recorded["update_before_add"] = update_before_add

        await sensor_module.async_setup_entry(hass, entry, async_add_entities)

        self.assertEqual(recorded["update_before_add"], False)
        self.assertEqual(len(recorded["entities"]), 1)

    async def test_sensor_schedules_initial_refresh_after_being_added(self):
        sensor = PublicTransportSensor(
            data=types.SimpleNamespace(info={}, last_trip_update_error=None),
            stop="1234",
            route="100214",
            name="Route 372",
            unique_id="test-unique-id",
        )
        calls = []
        sensor.async_schedule_update_ha_state = lambda force_refresh=False: calls.append(force_refresh)

        await sensor.async_added_to_hass()

        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
