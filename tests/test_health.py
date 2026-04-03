import datetime as dt
import importlib.util
import io
import sys
import types
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH_PATH = ROOT / "custom_components" / "gtfs_rt" / "health.py"
SPEC = importlib.util.spec_from_file_location("gtfs_rt_health", HEALTH_PATH)
HEALTH = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules.setdefault("requests", types.SimpleNamespace(get=None))
sys.modules[SPEC.name] = HEALTH
SPEC.loader.exec_module(HEALTH)

STATUS_INVALID_STOP = HEALTH.STATUS_INVALID_STOP
STATUS_NO_SERVICE_NOW = HEALTH.STATUS_NO_SERVICE_NOW
STATUS_NO_SERVICE_TODAY = HEALTH.STATUS_NO_SERVICE_TODAY
STATUS_ROUTE_STOP_MISMATCH = HEALTH.STATUS_ROUTE_STOP_MISMATCH
STATUS_SERVICE_EXPECTED = HEALTH.STATUS_SERVICE_EXPECTED
StaticScheduleValidator = HEALTH.StaticScheduleValidator


def build_archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class StaticScheduleValidatorTests(unittest.TestCase):
    def setUp(self):
        archive = build_archive(
            {
                "routes.txt": (
                    "route_id,agency_id,route_short_name,route_long_name,route_desc,route_type\n"
                    "100214,1,372,,,3\n"
                ),
                "stops.txt": (
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "23895,25th Ave NE & NE 75th St (SB),0,0\n"
                    "25797,25th Ave NE & NE 75th St (NB),0,0\n"
                ),
                "trips.txt": (
                    "route_id,service_id,trip_id,trip_headsign,direction_id,shape_id\n"
                    "100214,weekday,trip_one,U-District Station,1,shape\n"
                ),
                "stop_times.txt": (
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "trip_one,14:30:00,14:30:00,23895,1\n"
                    "trip_one,15:15:00,15:15:00,23895,2\n"
                ),
                "calendar.txt": (
                    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "weekday,1,1,1,1,1,0,0,20260101,20261231\n"
                ),
                "calendar_dates.txt": "service_id,date,exception_type\n",
            }
        )

        self.validator = StaticScheduleValidator(
            "https://example.com/google_transit.zip",
            [("100214", "23895"), ("100214", "25797"), ("100214", "99999")],
            refresh_interval=dt.timedelta(days=365),
        )
        self.validator._load_schedule_from_bytes(archive)
        self.validator._last_refresh = dt.datetime(2026, 4, 3, 14, 0, tzinfo=dt.timezone.utc)

    def test_service_expected_when_departure_is_imminent(self):
        now = dt.datetime(2026, 4, 3, 14, 0, tzinfo=dt.timezone.utc)
        status = self.validator.get_status("100214", "23895", now)

        self.assertEqual(status.status, STATUS_SERVICE_EXPECTED)
        self.assertTrue(status.service_today)
        self.assertTrue(status.service_expected_now)
        self.assertEqual(status.next_scheduled_departure.hour, 14)
        self.assertEqual(status.next_scheduled_departure.minute, 30)

    def test_no_service_now_when_next_trip_is_outside_window(self):
        now = dt.datetime(2026, 4, 3, 12, 0, tzinfo=dt.timezone.utc)
        status = self.validator.get_status("100214", "23895", now)

        self.assertEqual(status.status, STATUS_NO_SERVICE_NOW)
        self.assertTrue(status.service_today)
        self.assertFalse(status.service_expected_now)

    def test_no_service_today_on_weekend(self):
        now = dt.datetime(2026, 4, 4, 14, 0, tzinfo=dt.timezone.utc)
        status = self.validator.get_status("100214", "23895", now)

        self.assertEqual(status.status, STATUS_NO_SERVICE_TODAY)
        self.assertFalse(status.service_today)
        self.assertFalse(status.service_expected_now)

    def test_invalid_stop(self):
        now = dt.datetime(2026, 4, 3, 14, 0, tzinfo=dt.timezone.utc)
        status = self.validator.get_status("100214", "99999", now)

        self.assertEqual(status.status, STATUS_INVALID_STOP)
        self.assertTrue(status.route_exists)
        self.assertFalse(status.stop_exists)

    def test_route_stop_mismatch(self):
        now = dt.datetime(2026, 4, 3, 14, 0, tzinfo=dt.timezone.utc)
        status = self.validator.get_status("100214", "25797", now)

        self.assertEqual(status.status, STATUS_ROUTE_STOP_MISMATCH)
        self.assertTrue(status.route_exists)
        self.assertTrue(status.stop_exists)
        self.assertFalse(status.route_serves_stop)

    def test_route_label_comes_from_static_feed(self):
        now = dt.datetime(2026, 4, 3, 14, 0, tzinfo=dt.timezone.utc)
        self.validator.get_status("100214", "23895", now)

        self.assertEqual(self.validator.get_route_label("100214"), "372")


if __name__ == "__main__":
    unittest.main()
