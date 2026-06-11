import importlib.util
import sys
import types
import unittest
import datetime as dt
from email.utils import format_datetime
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
        self.assertEqual(attrs[sensor_module.ATTR_TRACKING_SOURCE], "gtfs_rt")
        self.assertEqual(attrs[sensor_module.ATTR_NEXT_TRACKING_SOURCE], "gtfs_rt")
        self.assertEqual(len(attrs[sensor_module.ATTR_UPCOMING_DEPARTURES]), 5)
        self.assertEqual(
            attrs[sensor_module.ATTR_UPCOMING_DEPARTURES][0],
            {
                "due_at": "16:02",
                "due_in": 2,
                "delay_minutes": 2.0,
                "occupancy": "FEW_SEATS_AVAILABLE",
                "tracking_source": "gtfs_rt",
                "is_realtime": True,
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
                "tracking_source": "gtfs_rt",
                "is_realtime": True,
            },
        )

    def test_transit_app_departures_merge_with_fallback_data(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("route-100", "stop-123")],
            static_schedule_url=None,
            stop_arrivals_url_template=None,
            transit_api_key="test-transit-key",
            transit_departures=[("route-100", "stop-123", "AGENCY:stop-123", "10")],
        )

        fallback_departure = StopDetails(now + dt.timedelta(minutes=12), None, None, None)

        def fallback_trip_updates(_positions, _vehicles_trips, _occupancy):
            data.info = {"route-100": {"stop-123": [fallback_departure]}}

        request_calls = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "route_departures": [
                        {
                            "global_stop_id": "AGENCY:stop-123",
                            "route_short_name": "10",
                            "itineraries": [
                                {
                                    "schedule_items": [
                                        {
                                            "departure_time": int(
                                                (now + dt.timedelta(minutes=4)).timestamp()
                                            ),
                                            "scheduled_departure_time": int(
                                                (now + dt.timedelta(minutes=3)).timestamp()
                                            ),
                                            "is_real_time": True,
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }

        def fake_get(url, **kwargs):
            request_calls.append((url, kwargs))
            return FakeResponse()

        data._update_route_statuses = fallback_trip_updates
        sensor_module.requests.get = fake_get

        data.update()

        self.assertEqual(
            request_calls[0][0],
            const_module.TRANSIT_API_STOP_DEPARTURES_URL,
        )
        self.assertEqual(request_calls[0][1]["headers"]["apiKey"], "test-transit-key")
        self.assertEqual(request_calls[0][1]["params"]["global_stop_ids"], "AGENCY:stop-123")
        departures = data.info["route-100"]["stop-123"]
        self.assertEqual(len(departures), 2)
        self.assertEqual(departures[0].arrival_time, now + dt.timedelta(minutes=4))
        self.assertEqual(departures[0].tracking_source, "transit_app")
        self.assertTrue(departures[0].is_realtime)
        self.assertEqual(departures[1], fallback_departure)
        self.assertIsNone(data.last_trip_update_error)

    def test_transit_app_departures_dedupe_fallback_departures(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("route-100", "stop-123")],
            static_schedule_url=None,
            stop_arrivals_url_template=None,
            transit_api_key="test-transit-key",
            transit_departures=[("route-100", "stop-123", "AGENCY:stop-123", "10")],
        )

        fallback_departure = StopDetails(
            now + dt.timedelta(minutes=4, seconds=30),
            None,
            None,
            None,
            trip_id="trip-1",
        )

        def fallback_trip_updates(_positions, _vehicles_trips, _occupancy):
            data.info = {"route-100": {"stop-123": [fallback_departure]}}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "route_departures": [
                        {
                            "global_stop_id": "AGENCY:stop-123",
                            "route_short_name": "10",
                            "itineraries": [
                                {
                                    "schedule_items": [
                                        {
                                            "departure_time": int(
                                                (now + dt.timedelta(minutes=4)).timestamp()
                                            ),
                                            "scheduled_departure_time": int(
                                                (now + dt.timedelta(minutes=3)).timestamp()
                                            ),
                                            "is_real_time": True,
                                            "rt_trip_id": "trip-1",
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }

        data._update_route_statuses = fallback_trip_updates
        sensor_module.requests.get = lambda *_args, **_kwargs: FakeResponse()

        data.update()

        departures = data.info["route-100"]["stop-123"]
        self.assertEqual(len(departures), 1)
        self.assertEqual(departures[0].arrival_time, now + dt.timedelta(minutes=4))
        self.assertEqual(departures[0].tracking_source, "transit_app")
        self.assertEqual(departures[0].trip_id, "trip-1")

    def test_transit_app_failure_keeps_fallback_data(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("route-100", "stop-123")],
            static_schedule_url=None,
            stop_arrivals_url_template=None,
            transit_api_key="test-transit-key",
            transit_departures=[("route-100", "stop-123", "AGENCY:stop-123", "10")],
        )
        fallback_departure = StopDetails(now + dt.timedelta(minutes=12), None, None, None)

        def fallback_trip_updates(_positions, _vehicles_trips, _occupancy):
            data.info = {"route-100": {"stop-123": [fallback_departure]}}

        def fail_get(*_args, **_kwargs):
            raise RuntimeError("transit down")

        data._update_route_statuses = fallback_trip_updates
        sensor_module.requests.get = fail_get

        data.update()

        self.assertEqual(data.info["route-100"]["stop-123"], [fallback_departure])
        self.assertIsNone(data.last_trip_update_error)

    def test_transit_app_batches_stop_ids_at_documented_limit(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        monitored_departures = [
            (f"route-{index}", f"stop-{index}")
            for index in range(101)
        ]
        transit_departures = [
            (
                f"route-{index}",
                f"stop-{index}",
                f"AGENCY:stop-{index}",
                "10",
            )
            for index in range(101)
        ]
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=monitored_departures,
            static_schedule_url=None,
            stop_arrivals_url_template=None,
            transit_api_key="test-transit-key",
            transit_departures=transit_departures,
        )
        request_params = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"route_departures": []}

        def fake_get(_url, **kwargs):
            request_params.append(kwargs["params"])
            return FakeResponse()

        data._update_route_statuses = lambda *_args: None
        sensor_module.requests.get = fake_get

        data.update()

        self.assertEqual(len(request_params), 2)
        self.assertEqual(len(request_params[0]["global_stop_ids"].split(",")), 100)
        self.assertEqual(len(request_params[1]["global_stop_ids"].split(",")), 1)
        self.assertEqual(request_params[0]["max_num_departures"], "5")
        self.assertEqual(request_params[0]["should_update_realtime"], "true")

    def test_transit_app_refresh_interval_reuses_cached_data(self):
        now = dt.datetime(2026, 4, 3, 16, 0, 0)
        dt_mod.now = lambda: now
        data = PublicTransportData(
            trip_update_url="https://example.com/tripupdates.pb",
            vehicle_position_url=None,
            headers={},
            monitored_departures=[("route-100", "stop-123")],
            static_schedule_url=None,
            stop_arrivals_url_template=None,
            transit_api_key="test-transit-key",
            transit_departures=[("route-100", "stop-123", "AGENCY:stop-123", "10")],
        )
        cached_departure = StopDetails(now + dt.timedelta(minutes=3), None, None, None)
        data._last_transit_app_info = {"route-100": {"stop-123": [cached_departure]}}
        data._transit_app_last_refresh = now - dt.timedelta(seconds=30)

        def unexpected_get(*_args, **_kwargs):
            raise AssertionError("Transit API should not be called inside refresh interval")

        sensor_module.requests.get = unexpected_get

        result = data._update_transit_app_statuses()

        self.assertIsNone(result)
        self.assertEqual(data.info, {"route-100": {"stop-123": [cached_departure]}})

    def test_retry_after_accepts_http_date(self):
        retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=120)

        class FakeResponse:
            headers = {"Retry-After": format_datetime(retry_at, usegmt=True)}

        retry_after = PublicTransportData._get_stop_arrivals_retry_after(FakeResponse())

        self.assertGreaterEqual(retry_after, dt.timedelta(seconds=90))
        self.assertLessEqual(retry_after, dt.timedelta(seconds=120))

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
