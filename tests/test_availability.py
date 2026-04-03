import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.modules.setdefault("requests", types.SimpleNamespace(get=None))

HEALTH_SPEC = importlib.util.spec_from_file_location(
    "gtfs_rt_health",
    ROOT / "custom_components" / "gtfs_rt" / "health.py",
)
HEALTH = importlib.util.module_from_spec(HEALTH_SPEC)
assert HEALTH_SPEC and HEALTH_SPEC.loader
sys.modules[HEALTH_SPEC.name] = HEALTH
HEALTH_SPEC.loader.exec_module(HEALTH)

PACKAGE = types.ModuleType("custom_components.gtfs_rt")
PACKAGE.__path__ = [str(ROOT / "custom_components" / "gtfs_rt")]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.gtfs_rt"] = PACKAGE
sys.modules["custom_components.gtfs_rt.health"] = HEALTH

AVAILABILITY_SPEC = importlib.util.spec_from_file_location(
    "custom_components.gtfs_rt.availability",
    ROOT / "custom_components" / "gtfs_rt" / "availability.py",
)
AVAILABILITY = importlib.util.module_from_spec(AVAILABILITY_SPEC)
assert AVAILABILITY_SPEC and AVAILABILITY_SPEC.loader
sys.modules[AVAILABILITY_SPEC.name] = AVAILABILITY
AVAILABILITY_SPEC.loader.exec_module(AVAILABILITY)

ScheduleStatus = HEALTH.ScheduleStatus
STATUS_INVALID_STOP = HEALTH.STATUS_INVALID_STOP
STATUS_SERVICE_EXPECTED = HEALTH.STATUS_SERVICE_EXPECTED
should_mark_entity_unavailable = AVAILABILITY.should_mark_entity_unavailable


class AvailabilityTests(unittest.TestCase):
    def test_trip_update_error_marks_entity_unavailable(self):
        self.assertTrue(
            should_mark_entity_unavailable(
                last_trip_update_error="timeout",
                schedule_status=None,
                has_realtime_departures=False,
            )
        )

    def test_invalid_stop_marks_entity_unavailable(self):
        schedule_status = ScheduleStatus(
            status=STATUS_INVALID_STOP,
            route_exists=True,
            stop_exists=False,
            route_serves_stop=False,
            service_today=False,
            service_expected_now=False,
            next_scheduled_departure=None,
            problem_reason="bad stop",
        )

        self.assertTrue(
            should_mark_entity_unavailable(
                last_trip_update_error=None,
                schedule_status=schedule_status,
                has_realtime_departures=False,
            )
        )

    def test_missing_realtime_departures_during_service_stays_available(self):
        schedule_status = ScheduleStatus(
            status=STATUS_SERVICE_EXPECTED,
            route_exists=True,
            stop_exists=True,
            route_serves_stop=True,
            service_today=True,
            service_expected_now=True,
            next_scheduled_departure=None,
            problem_reason=None,
        )

        self.assertFalse(
            should_mark_entity_unavailable(
                last_trip_update_error=None,
                schedule_status=schedule_status,
                has_realtime_departures=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
