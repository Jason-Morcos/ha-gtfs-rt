"""Frequency templates and boardable untimed stops use their actual calendars."""

import datetime as dt
import unittest

from test_health import StaticScheduleValidator, build_archive

UTC = dt.timezone.utc


def schedule(
    *,
    exact="1",
    start="08:00:00",
    end="10:00:00",
    headway=600,
    timezone="UTC",
    service_date="20260403",
    untimed=False,
    pickup="0",
):
    middle = "," if untimed else "05:10:00,05:10:00"
    files = {
        "agency.txt": (
            "agency_id,agency_name,agency_url,agency_timezone\n"
            f"A,Example,https://example.org,{timezone}\n"
        ),
        "routes.txt": "route_id,agency_id,route_short_name,route_type\nR,A,10,3\n",
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "A,Start,1,1\nS,Middle,1,1\nZ,End,1,1\n"
        ),
        "trips.txt": "route_id,service_id,trip_id\nR,service,trip\n",
        # Rows are deliberately unordered; the first stop is not monitored.
        # Nonzero template start proves offsets are relative, not absolute.
        "stop_times.txt": (
            "trip_id,stop_id,stop_sequence,arrival_time,departure_time,timepoint,pickup_type\n"
            "trip,Z,30,05:20:00,05:20:00,1,0\n"
            f"trip,S,20,{middle},{0 if untimed else 1},{pickup}\n"
            "trip,A,10,05:00:00,05:00:00,1,0\n"
        ),
        "calendar_dates.txt": f"service_id,date,exception_type\nservice,{service_date},1\n",
    }
    if exact is not None:
        files["frequencies.txt"] = (
            "trip_id,start_time,end_time,headway_secs,exact_times\n"
            f"trip,{start},{end},{headway},{exact}\n"
        )
    return files


def load(files, now, *, all_routes=False, window=None):
    kwargs = {}
    if window is not None:
        kwargs = {"active_window_before": window, "active_window_after": window}
    data = StaticScheduleValidator(
        "https://example.org/static.zip", [("" if all_routes else "R", "S")], **kwargs
    )
    data._load_schedule_from_bytes(build_archive(files))
    data._last_refresh = now
    return data


class FrequencyScheduleTests(unittest.TestCase):
    def test_exact_frequency_uses_unordered_first_stop_offset_and_no_template_departure(
        self,
    ):
        now = dt.datetime(2026, 4, 3, 9, tzinfo=UTC)
        data = load(schedule(), now)
        status = data.get_status("R", "S", now)
        self.assertTrue(status.service_today)
        self.assertTrue(status.service_expected_now)
        self.assertEqual(status.next_scheduled_departure, now)
        before = data.get_status("R", "S", now.replace(hour=5, minute=5))
        self.assertFalse(before.service_expected_now)
        self.assertEqual(
            before.next_scheduled_departure, now.replace(hour=8, minute=10)
        )
        after = data.get_status("R", "S", now.replace(hour=12))
        self.assertFalse(after.service_expected_now)
        self.assertIsNone(after.next_scheduled_departure)
        self.assertFalse(data._departures_by_service)

    def test_exact_end_is_exclusive_and_fractional_now_advances_to_next_run(self):
        now = dt.datetime(2026, 4, 3, 9, tzinfo=UTC)
        data = load(schedule(), now, window=dt.timedelta(0))
        fractional = data.get_status("R", "S", now.replace(microsecond=1))
        self.assertFalse(fractional.service_expected_now)
        self.assertEqual(fractional.next_scheduled_departure, now.replace(minute=10))
        last = data.get_status("R", "S", now.replace(hour=10))
        self.assertTrue(last.service_expected_now)
        self.assertEqual(last.next_scheduled_departure, now.replace(hour=10))
        excluded = data.get_status("R", "S", now.replace(hour=10, minute=10))
        self.assertFalse(excluded.service_expected_now)
        self.assertIsNone(excluded.next_scheduled_departure)

    def test_non_exact_frequency_reports_window_without_precise_departure(self):
        now = dt.datetime(2026, 4, 3, 9, tzinfo=UTC)
        for exact in ("0", ""):
            with self.subTest(exact=exact):
                data = load(
                    schedule(exact=exact), now, all_routes=True, window=dt.timedelta(0)
                )
                active = data.get_status("", "S", now)
                self.assertTrue(active.service_today)
                self.assertTrue(active.service_expected_now)
                self.assertIsNone(active.next_scheduled_departure)
                before = data.get_status("", "S", now.replace(hour=8, minute=9))
                self.assertFalse(before.service_expected_now)
                ending = data.get_status("", "S", now.replace(hour=10, minute=10))
                self.assertFalse(ending.service_expected_now)
                self.assertIsNone(ending.next_scheduled_departure)

    def test_separate_frequency_windows_keep_gap_and_next_scheduled_run(self):
        files = schedule(end="09:00:00")
        files["frequencies.txt"] += "trip,10:00:00,11:00:00,600,1\n"
        now = dt.datetime(2026, 4, 3, 9, 30, tzinfo=UTC)
        data = load(files, now, window=dt.timedelta(0))
        status = data.get_status("R", "S", now)
        self.assertFalse(status.service_expected_now)
        self.assertEqual(
            status.next_scheduled_departure, now.replace(hour=10, minute=10)
        )
        self.assertEqual(
            len(data._frequency_windows_by_service[("R", "S", "service")]), 2
        )

    def test_frequency_carryover_and_calendar_removal(self):
        now = dt.datetime(2026, 4, 4, 1, tzinfo=UTC)
        for exact in ("0", "1"):
            with self.subTest(exact=exact):
                files = schedule(exact=exact, start="24:00:00", end="26:00:00")
                data = load(files, now)
                status = data.get_status("R", "S", now)
                self.assertTrue(status.service_today)
                self.assertTrue(status.service_expected_now)
                self.assertEqual(
                    status.next_scheduled_departure, now if exact == "1" else None
                )
                files["calendar_dates.txt"] = (
                    "service_id,date,exception_type\nservice,20260403,2\n"
                )
                removed = load(files, now).get_status("R", "S", now)
                self.assertFalse(removed.service_today)
                self.assertFalse(removed.service_expected_now)
                self.assertIsNone(removed.next_scheduled_departure)

    def test_frequency_dst_uses_elapsed_service_day_seconds(self):
        cases = (
            # Spring noon-minus-12 anchor is 22:00 UTC on the preceding date.
            ("20260329", dt.datetime(2026, 3, 28, 23, 15, tzinfo=UTC)),
            # Fall noon-minus-12 anchor is 23:00 UTC on the preceding date.
            ("20261025", dt.datetime(2026, 10, 25, 0, 15, tzinfo=UTC)),
        )
        for service_date, now in cases:
            for exact in ("0", "1"):
                with self.subTest(service_date=service_date, exact=exact):
                    files = schedule(
                        exact=exact,
                        start="01:00:00",
                        end="02:00:00",
                        timezone="Europe/Berlin",
                        service_date=service_date,
                    )
                    status = load(files, now).get_status("R", "S", now)
                    self.assertTrue(status.service_expected_now)
                    expected = now.replace(minute=20) if exact == "1" else None
                    self.assertEqual(status.next_scheduled_departure, expected)

    def test_next_day_frequency_can_be_expected_before_midnight(self):
        now = dt.datetime(2026, 4, 3, 23, 50, tzinfo=UTC)
        for exact in ("0", "1"):
            with self.subTest(exact=exact):
                files = schedule(
                    exact=exact,
                    start="00:05:00",
                    end="01:00:00",
                    service_date="20260404",
                )
                status = load(files, now).get_status("R", "S", now)
                self.assertTrue(status.service_expected_now)
                self.assertFalse(status.service_today)
                expected = (
                    dt.datetime(2026, 4, 4, 0, 15, tzinfo=UTC) if exact == "1" else None
                )
                self.assertEqual(status.next_scheduled_departure, expected)

    def test_dense_extended_frequency_remains_compressed_and_snapshot_survives_failure(
        self,
    ):
        now = dt.datetime(2026, 4, 3, 9, tzinfo=UTC)
        files = schedule(start="00:00:00", end="100:00:00", headway=1)
        data = load(files, now)
        self.assertEqual(
            len(data._frequency_windows_by_service[("R", "S", "service")]), 1
        )
        self.assertFalse(data._departures_by_service)
        broken = dict(files)
        broken.pop("stop_times.txt")
        with self.assertRaises(KeyError):
            data._load_schedule_from_bytes(build_archive(broken))
        status = data.get_status("R", "S", now)
        self.assertTrue(status.service_expected_now)
        self.assertEqual(status.next_scheduled_departure, now)

    def test_nonboardable_frequency_stop_keeps_membership_without_service(self):
        now = dt.datetime(2026, 4, 3, 9, tzinfo=UTC)
        status = load(schedule(pickup="1"), now).get_status("R", "S", now)
        self.assertTrue(status.route_serves_stop)
        self.assertFalse(status.service_today)
        self.assertFalse(status.service_expected_now)
        self.assertIsNone(status.next_scheduled_departure)

    def test_boardable_untimed_stop_tracks_active_calendar_without_inventing_time(self):
        now = dt.datetime(2026, 4, 3, 5, 10, tzinfo=UTC)
        for exact in (None, "0", "1"):
            for all_routes in (False, True):
                with self.subTest(exact=exact, all_routes=all_routes):
                    files = schedule(exact=exact, untimed=True)
                    route = "" if all_routes else "R"
                    status = load(files, now, all_routes=all_routes).get_status(
                        route, "S", now
                    )
                    self.assertTrue(status.route_serves_stop)
                    self.assertTrue(status.service_today)
                    self.assertFalse(status.service_expected_now)
                    self.assertIsNone(status.next_scheduled_departure)
                    files["calendar_dates.txt"] = (
                        "service_id,date,exception_type\nservice,20260403,2\n"
                    )
                    removed = load(files, now, all_routes=all_routes).get_status(
                        route, "S", now
                    )
                    self.assertFalse(removed.service_today)
                    self.assertIsNone(removed.next_scheduled_departure)


if __name__ == "__main__":
    unittest.main()
