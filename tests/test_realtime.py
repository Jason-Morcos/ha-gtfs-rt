import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REALTIME_PATH = ROOT / "custom_components" / "gtfs_rt" / "realtime.py"
SPEC = importlib.util.spec_from_file_location("gtfs_rt_realtime", REALTIME_PATH)
REALTIME = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = REALTIME
SPEC.loader.exec_module(REALTIME)

filter_onebusaway_arrivals = REALTIME.filter_onebusaway_arrivals
filter_transit_app_departures = REALTIME.filter_transit_app_departures
combine_duplicate_departures = REALTIME.combine_duplicate_departures
route_id_matches = REALTIME.route_id_matches
StopDetails = REALTIME.StopDetails


class RealtimeTests(unittest.TestCase):
    def test_route_id_matches_agency_prefixed_ids(self):
        self.assertTrue(route_id_matches("100214", "1_100214"))
        self.assertTrue(route_id_matches("1_100214", "1_100214"))
        self.assertFalse(route_id_matches("1_100214", "100214"))
        self.assertFalse(route_id_matches("1_100214", "2_100214"))
        self.assertFalse(route_id_matches("100214", "1_100341"))

    def test_filter_onebusaway_arrivals_uses_future_scheduled_or_predicted_times(self):
        now = dt.datetime(2026, 4, 3, 15, 0, 0)
        arrivals = [
            {
                "routeId": "1_100214",
                "predicted": True,
                "predictedArrivalTime": 1775258100000,
                "scheduledArrivalTime": 1775258040000,
                "tripStatus": {
                    "position": {"lat": 47.0, "lon": -122.0},
                    "occupancyStatus": "MANY_SEATS_AVAILABLE",
                },
                "tripId": "AGENCY_trip-1",
            },
            {
                "routeId": "1_100214",
                "predicted": False,
                "predictedArrivalTime": 0,
                "scheduledArrivalTime": 1775259000000,
                "tripStatus": {},
            },
            {
                "routeId": "1_100341",
                "predicted": True,
                "predictedArrivalTime": 1775257800000,
                "scheduledArrivalTime": 1775257740000,
            },
            {
                "routeId": "1_100214",
                "predicted": True,
                "predictedArrivalTime": 1775253300000,
                "scheduledArrivalTime": 1775253240000,
            },
        ]

        details = filter_onebusaway_arrivals(arrivals, "100214", now)

        self.assertEqual(len(details), 2)
        self.assertEqual(details[0].arrival_time, dt.datetime.fromtimestamp(1775258100))
        self.assertEqual(details[0].delay, 60)
        self.assertEqual(details[0].position.latitude, 47.0)
        self.assertEqual(details[0].tracking_source, REALTIME.TRACKING_SOURCE_ONEBUSAWAY)
        self.assertTrue(details[0].is_realtime)
        self.assertEqual(details[0].trip_id, "AGENCY_trip-1")
        self.assertEqual(details[1].arrival_time, dt.datetime.fromtimestamp(1775259000))
        self.assertIsNone(details[1].delay)
        self.assertEqual(details[1].tracking_source, REALTIME.TRACKING_SOURCE_SCHEDULE)
        self.assertFalse(details[1].is_realtime)

    def test_filter_transit_app_departures_uses_realtime_source_metadata(self):
        now = dt.datetime(2026, 4, 3, 15, 0, 0)
        departures = [
            {
                "global_stop_id": "AGENCY:1234",
                "route_short_name": "10",
                "itineraries": [
                    {
                        "schedule_items": [
                            {
                                "departure_time": int((now + dt.timedelta(minutes=3)).timestamp()),
                                "scheduled_departure_time": int((now + dt.timedelta(minutes=2)).timestamp()),
                                "is_real_time": True,
                                "rt_trip_id": "trip-1",
                            },
                            {
                                "departure_time": int((now + dt.timedelta(minutes=8)).timestamp()),
                                "is_real_time": False,
                            },
                            {
                                "departure_time": int((now - dt.timedelta(minutes=1)).timestamp()),
                                "is_real_time": True,
                            },
                            {
                                "departure_time": int((now + dt.timedelta(minutes=12)).timestamp()),
                                "is_real_time": True,
                                "is_cancelled": True,
                            },
                        ]
                    }
                ],
            },
            {
                "global_stop_id": "AGENCY:1234",
                "route_short_name": "20",
                "itineraries": [
                    {
                        "schedule_items": [
                            {
                                "departure_time": int((now + dt.timedelta(minutes=4)).timestamp()),
                                "is_real_time": True,
                            }
                        ]
                    }
                ],
            },
        ]

        details = filter_transit_app_departures(
            departures,
            global_stop_id="AGENCY:1234",
            configured_route="10",
            now=now,
        )

        self.assertEqual(len(details), 2)
        self.assertEqual(details[0].arrival_time, now + dt.timedelta(minutes=3))
        self.assertEqual(details[0].delay, 60)
        self.assertEqual(details[0].tracking_source, REALTIME.TRACKING_SOURCE_TRANSIT_APP)
        self.assertTrue(details[0].is_realtime)
        self.assertEqual(details[0].trip_id, "trip-1")
        self.assertEqual(details[1].arrival_time, now + dt.timedelta(minutes=8))
        self.assertEqual(details[1].tracking_source, REALTIME.TRACKING_SOURCE_SCHEDULE)
        self.assertFalse(details[1].is_realtime)

    def test_filter_transit_app_departures_reads_v4_merged_itineraries(self):
        now = dt.datetime(2026, 4, 3, 15, 0, 0)
        departures = [
            {
                "global_stop_id": "AGENCY:1234",
                "route_id": "route-10",
                "merged_itineraries": [
                    {
                        "schedule_items": [
                            {
                                "departure_time": int((now + dt.timedelta(minutes=6)).timestamp()),
                                "is_real_time": True,
                                "trip_search_key": "trip-v4",
                            }
                        ]
                    }
                ],
            }
        ]

        details = filter_transit_app_departures(
            departures,
            global_stop_id="AGENCY:1234",
            configured_route="route-10",
            now=now,
        )

        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].arrival_time, now + dt.timedelta(minutes=6))
        self.assertEqual(details[0].tracking_source, REALTIME.TRACKING_SOURCE_TRANSIT_APP)
        self.assertEqual(details[0].trip_id, "trip-v4")

    def test_combine_duplicate_departures_prefers_better_cross_source_detail(self):
        now = dt.datetime(2026, 4, 3, 15, 0, 0)
        schedule_detail = StopDetails(
            now + dt.timedelta(seconds=20),
            None,
            None,
            None,
            REALTIME.TRACKING_SOURCE_SCHEDULE,
            False,
        )
        transit_detail = StopDetails(
            now + dt.timedelta(seconds=65),
            None,
            None,
            45,
            REALTIME.TRACKING_SOURCE_TRANSIT_APP,
            True,
        )
        later_detail = StopDetails(
            now + dt.timedelta(minutes=5),
            None,
            None,
            None,
            REALTIME.TRACKING_SOURCE_SCHEDULE,
            False,
        )

        details = combine_duplicate_departures([later_detail, schedule_detail, transit_detail])

        self.assertEqual(details, [transit_detail, later_detail])

    def test_combine_duplicate_departures_keeps_close_same_source_headways(self):
        now = dt.datetime(2026, 4, 3, 15, 0, 0)
        first_detail = StopDetails(
            now + dt.timedelta(seconds=20),
            None,
            None,
            None,
            REALTIME.TRACKING_SOURCE_TRANSIT_APP,
            True,
        )
        second_detail = StopDetails(
            now + dt.timedelta(seconds=65),
            None,
            None,
            None,
            REALTIME.TRACKING_SOURCE_TRANSIT_APP,
            True,
        )

        details = combine_duplicate_departures([second_detail, first_detail])

        self.assertEqual(details, [first_detail, second_detail])


if __name__ == "__main__":
    unittest.main()
