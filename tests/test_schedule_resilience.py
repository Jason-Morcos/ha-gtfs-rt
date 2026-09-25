"""Service-day and failure regressions using synthetic agency data."""
import datetime as dt
import unittest
from unittest.mock import patch

from test_health import HEALTH, StaticScheduleValidator, build_archive

UTC = dt.timezone.utc


def schedule_files(*, departure="25:10:00", timezone="Europe/Berlin"):
    return {
        "agency.txt": f"agency_id,agency_name,agency_url,agency_timezone\nA,Example,https://example.org,{timezone}\n",
        "routes.txt": "route_id,route_short_name\nR,10\n",
        "stops.txt": "stop_id,stop_name\nS,Example Stop\n",
        "trips.txt": "route_id,service_id,trip_id\nR,service,trip\n",
        "stop_times.txt": f"trip_id,stop_id,arrival_time,departure_time\ntrip,S,{departure},{departure}\n",
        "calendar_dates.txt": "service_id,date,exception_type\nservice,20260403,1\n",
    }


def validator(files, now):
    result = StaticScheduleValidator("https://example.org/feed.zip?key=private", [("R", "S")])
    result._load_schedule_from_bytes(build_archive(files))
    result._last_refresh = now
    return result


class ScheduleResilienceTests(unittest.TestCase):
    def test_calendar_dates_only_and_previous_service_day_after_midnight(self):
        now = dt.datetime(2026, 4, 3, 22, 45, tzinfo=UTC)  # 00:45 Saturday at agency
        status = validator(schedule_files(), now).get_status("R", "S", now)
        self.assertEqual(status.status, HEALTH.STATUS_SERVICE_EXPECTED)
        self.assertTrue(status.service_today)
        self.assertEqual(status.next_scheduled_departure, dt.datetime(2026, 4, 3, 23, 10, tzinfo=UTC))

    def test_calendar_only_is_valid(self):
        files = schedule_files(departure="14:00:00")
        files.pop("calendar_dates.txt")
        files["calendar.txt"] = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "service,1,1,1,1,1,0,0,20260101,20261231\n"
        )
        now = dt.datetime(2026, 4, 3, 11, 45, tzinfo=UTC)
        self.assertEqual(validator(files, now).get_status("R", "S", now).status, HEALTH.STATUS_SERVICE_EXPECTED)

    def test_untimed_stop_is_valid_without_inventing_a_departure(self):
        files = schedule_files(departure="")
        now = dt.datetime(2026, 4, 3, 12, tzinfo=UTC)
        status = validator(files, now).get_status("R", "S", now)
        self.assertTrue(status.route_serves_stop)
        self.assertFalse(status.is_config_problem)
        self.assertIsNone(status.next_scheduled_departure)

    def test_dropoff_only_stop_does_not_offer_a_departure(self):
        files = schedule_files(departure="14:00:00")
        files["stop_times.txt"] = "trip_id,stop_id,arrival_time,departure_time,pickup_type\ntrip,S,14:00:00,14:00:00,1\n"
        now = dt.datetime(2026, 4, 3, 11, 45, tzinfo=UTC)
        status = validator(files, now).get_status("R", "S", now)
        self.assertTrue(status.route_serves_stop)
        self.assertFalse(status.service_expected_now)

    def test_calendar_exception_removes_previous_day_service(self):
        files = schedule_files()
        files["calendar_dates.txt"] = "service_id,date,exception_type\nservice,20260403,2\n"
        now = dt.datetime(2026, 4, 3, 22, 45, tzinfo=UTC)
        self.assertEqual(validator(files, now).get_status("R", "S", now).status, HEALTH.STATUS_NO_SERVICE_TODAY)

    def test_missing_both_calendars_rejected_without_losing_previous_snapshot(self):
        now = dt.datetime(2026, 4, 3, 22, 45, tzinfo=UTC)
        data = validator(schedule_files(), now)
        files = schedule_files()
        files.pop("calendar_dates.txt")
        with self.assertRaises(ValueError):
            data._load_schedule_from_bytes(build_archive(files))
        self.assertEqual(data.get_status("R", "S", now).status, HEALTH.STATUS_SERVICE_EXPECTED)

    def test_partial_parse_failure_preserves_previous_snapshot(self):
        now = dt.datetime(2026, 4, 3, 22, 45, tzinfo=UTC)
        data = validator(schedule_files(), now)
        files = schedule_files()
        files["routes.txt"] = "route_id,route_short_name\nother,99\n"
        files.pop("stop_times.txt")
        with self.assertRaises(KeyError):
            data._load_schedule_from_bytes(build_archive(files))
        self.assertEqual(data.get_route_label("R"), "10")
        self.assertEqual(data.get_status("R", "S", now).status, HEALTH.STATUS_SERVICE_EXPECTED)

    def test_failed_download_retries_once_per_backoff_not_per_sensor(self):
        now = dt.datetime(2026, 4, 3, 22, 45, tzinfo=UTC)
        data = StaticScheduleValidator("https://example.org/?key=private", [("R", "S")])
        with patch.object(HEALTH.requests, "get", side_effect=RuntimeError("https://example.org/?key=private")) as get:
            with self.assertLogs(HEALTH._LOGGER, level="WARNING") as logs:
                first = data.get_status("R", "S", now)
                second = data.get_status("R", "S", now + dt.timedelta(minutes=1))
            self.assertEqual(get.call_count, 1)
            self.assertEqual(first.status, HEALTH.STATUS_LOOKUP_FAILED)
            self.assertEqual(second.status, HEALTH.STATUS_LOOKUP_FAILED)
            self.assertNotIn("private", first.problem_reason + str(logs.output))
            data.get_status("R", "S", now + dt.timedelta(minutes=5))
            self.assertEqual(get.call_count, 2)
        response = unittest.mock.Mock(content=build_archive(schedule_files()))
        with patch.object(HEALTH.requests, "get", return_value=response):
            status = data.get_status("R", "S", now + dt.timedelta(minutes=10))
        self.assertEqual(status.status, HEALTH.STATUS_SERVICE_EXPECTED)

    def test_dst_service_time_uses_elapsed_time_from_noon_minus_twelve_hours(self):
        files = schedule_files(departure="01:30:00")
        files["calendar_dates.txt"] = "service_id,date,exception_type\nservice,20261025,1\n"
        now = dt.datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
        status = validator(files, now).get_status("R", "S", now)
        # On this fall-back day noon is 11:00 UTC; service-day zero is 23:00 UTC.
        self.assertEqual(status.next_scheduled_departure, dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
        self.assertTrue(status.service_expected_now)

    def test_next_service_day_near_midnight_is_expected(self):
        files = schedule_files(departure="00:15:00", timezone="UTC")
        files["calendar_dates.txt"] = "service_id,date,exception_type\nservice,20260404,1\n"
        now = dt.datetime(2026, 4, 3, 23, 50, tzinfo=UTC)
        status = validator(files, now).get_status("R", "S", now)
        self.assertTrue(status.service_expected_now)
        self.assertFalse(status.service_today)
        self.assertEqual(status.next_scheduled_departure, dt.datetime(2026, 4, 4, 0, 15, tzinfo=UTC))
