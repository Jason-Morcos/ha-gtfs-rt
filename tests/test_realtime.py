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
route_id_matches = REALTIME.route_id_matches


class RealtimeTests(unittest.TestCase):
    def test_route_id_matches_agency_prefixed_ids(self):
        self.assertTrue(route_id_matches("100214", "1_100214"))
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
        self.assertEqual(details[1].arrival_time, dt.datetime.fromtimestamp(1775259000))
        self.assertIsNone(details[1].delay)


if __name__ == "__main__":
    unittest.main()
