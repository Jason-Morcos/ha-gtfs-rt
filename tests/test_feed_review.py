"""Required feed structure and cross-feed vehicle instance ambiguity."""

import datetime as dt
import sys
import unittest
from unittest.mock import patch

from google.protobuf import json_format
from google.transit import gtfs_realtime_pb2 as pb
import test_sensor as fixture

FEED, SENSOR = sys.modules["custom_components.gtfs_rt.feed"], fixture.sensor_module
NOW = dt.datetime(2026, 4, 3, 12, tzinfo=dt.timezone.utc)


def feed():
    value = pb.FeedMessage()
    value.header.gtfs_realtime_version = "2.0"
    return value


class FeedReviewTests(unittest.TestCase):
    def test_missing_or_uninitialized_header_is_rejected_in_binary_and_json(self):
        for header in (False, True):
            value = pb.FeedMessage()
            if header:
                value.header.timestamp = int(NOW.timestamp())
            value.entity.add(id="trip").trip_update.trip.trip_id = "T"
            for content in (
                value.SerializePartialToString(),
                json_format.MessageToJson(value).encode(),
            ):
                with self.subTest(header=header, content=content[:1]):
                    with self.assertRaises(FEED.InvalidFeedError):
                        FEED.parse_feed(content)
        self.assertEqual(
            FEED.parse_feed(feed().SerializeToString()).header.gtfs_realtime_version,
            "2.0",
        )

    def test_missing_required_entity_fields_are_rejected(self):
        value = feed()
        value.entity.add().trip_update.trip.trip_id = "T"  # entity.id is required
        with self.assertRaises(FEED.InvalidFeedError):
            FEED.parse_feed(value.SerializePartialToString())

    def test_underspecified_vehicle_is_not_shared_across_frequency_instances(self):
        cases = [
            ("", "", [("20260403", "08:00:00"), ("20260403", "08:10:00")], False),
            (
                "20260403",
                "",
                [("20260403", "08:00:00"), ("20260403", "08:10:00")],
                False,
            ),
            (
                "",
                "08:00:00",
                [("20260403", "08:00:00"), ("20260404", "08:00:00")],
                False,
            ),
            (
                "20260403",
                "08:00:00",
                [("20260403", "08:00:00"), ("20260403", "08:10:00")],
                True,
            ),
            ("", "", [("20260403", "08:00:00")], True),
        ]
        for date, time, instances, can_enrich in cases:
            with self.subTest(date=date, time=time, instances=instances):
                vehicles = feed()
                vehicle = vehicles.entity.add(id="vehicle").vehicle
                vehicle.vehicle.id, vehicle.trip.trip_id, vehicle.trip.route_id = (
                    "V",
                    "T",
                    "R",
                )
                vehicle.trip.start_date, vehicle.trip.start_time = date, time
                vehicle.position.latitude, vehicle.position.longitude = 1, 2
                vehicle.occupancy_status = pb.VehiclePosition.EMPTY
                index = FEED.VehicleIndex(vehicles)
                trips = feed()
                for i, (start_date, start_time) in enumerate(instances):
                    update = trips.entity.add(id=str(i)).trip_update
                    update.trip.trip_id = "T"
                    update.trip.start_date, update.trip.start_time = (
                        start_date,
                        start_time,
                    )
                    update.stop_time_update.add(stop_id="S").departure.time = int(
                        (NOW + dt.timedelta(minutes=5 + i)).timestamp()
                    )
                data = SENSOR.PublicTransportData("https://example.org/trips")
                with (
                    patch.object(SENSOR, "fetch_feed", return_value=trips),
                    patch.object(fixture.dt_mod, "now", return_value=NOW),
                ):
                    data._update_route_statuses(
                        {"V": fixture.RealtimePosition(1, 2)}, index, {"V": "EMPTY"}
                    )
                first = data.info.get("R", {}).get("S", [])
                if can_enrich:
                    self.assertEqual(len(first), 1)
                    self.assertIsNotNone(first[0].position)
                    self.assertEqual(first[0].occupancy, "EMPTY")
                else:
                    self.assertEqual(len(data.info[""]["S"]), len(instances))
                    self.assertTrue(
                        all(
                            x.position is None and x.occupancy is None
                            for x in data.info[""]["S"]
                        )
                    )
                # Explicit route remains available even when vehicle enrichment is ambiguous.
                for update in trips.entity:
                    update.trip_update.trip.route_id = "R"
                with (
                    patch.object(SENSOR, "fetch_feed", return_value=trips),
                    patch.object(fixture.dt_mod, "now", return_value=NOW),
                ):
                    data._update_route_statuses({}, index, {})
                self.assertEqual(len(data.info["R"]["S"]), len(instances))
