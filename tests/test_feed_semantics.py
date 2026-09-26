"""Exercise the pinned protobuf decoder and timezone-independent sensor output."""
import datetime as dt
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2 as pb
import test_sensor as fixture

SENSOR = fixture.sensor_module
REALTIME = fixture.realtime_module
UTC = dt.timezone.utc


class FeedSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
        self.clock = patch.object(fixture.dt_mod, "now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.data = SENSOR.PublicTransportData("https://example.org/trips?key=private")
        self.feed = pb.FeedMessage()
        self.feed.header.gtfs_realtime_version = "2.0"

    def add_trip(self, identifier, *, relationship=pb.TripDescriptor.SCHEDULED):
        entity = self.feed.entity.add(id=identifier)
        entity.trip_update.trip.trip_id = identifier
        entity.trip_update.trip.route_id = "R"
        entity.trip_update.trip.schedule_relationship = relationship
        stop = entity.trip_update.stop_time_update.add(stop_id="S")
        stop.departure.time = int(self.now.timestamp()) + 300
        return entity, stop

    def refresh(self):
        with patch.object(SENSOR.requests, "get", return_value=Mock(content=self.feed.SerializeToString())):
            self.data._update_route_statuses({}, {}, {})
        return self.data.info.get("R", {}).get("S", [])

    def test_cancelled_deleted_entity_and_skipped_or_no_data_stops_do_not_appear(self):
        self.add_trip("cancelled", relationship=pb.TripDescriptor.CANCELED)
        entity, _ = self.add_trip("deleted")
        entity.is_deleted = True
        _, stop = self.add_trip("skipped")
        stop.schedule_relationship = pb.TripUpdate.StopTimeUpdate.SKIPPED
        _, stop = self.add_trip("no-data")
        stop.schedule_relationship = pb.TripUpdate.StopTimeUpdate.NO_DATA
        self.add_trip("valid")
        self.assertEqual([item.trip_id for item in self.refresh()], ["valid"])

    def test_arrival_only_supported_but_past_departure_is_not_resurrected(self):
        _, stop = self.add_trip("arrival-only")
        stop.ClearField("departure")
        stop.arrival.time = int(self.now.timestamp()) + 60
        _, stop = self.add_trip("already-left")
        stop.departure.time = int(self.now.timestamp()) - 60
        stop.arrival.time = int(self.now.timestamp()) + 60
        self.assertEqual([item.trip_id for item in self.refresh()], ["arrival-only"])

    def test_zero_delay_is_distinct_from_missing_delay(self):
        _, stop = self.add_trip("on-time")
        stop.departure.delay = 0
        self.add_trip("unknown-delay")
        details = {item.trip_id: item for item in self.refresh()}
        self.assertEqual(SENSOR.departure_attributes(details["on-time"])["delay_minutes"], 0)
        self.assertIsNone(SENSOR.departure_attributes(details["unknown-delay"])["delay_minutes"])

    def test_vehicle_without_optional_occupancy_position_or_route_does_not_invent_data(self):
        self.data._vehicle_position_url = "https://example.org/vehicles"
        first = self.feed.entity.add(id="first").vehicle
        first.vehicle.id = "V1"
        first.trip.trip_id = "trip-1"  # route_id is optional
        second = self.feed.entity.add(id="second").vehicle
        second.vehicle.id = "V2"
        second.trip.trip_id = "trip-2"
        second.occupancy_status = pb.VehiclePosition.EMPTY
        second.position.latitude = 1
        second.position.longitude = 2
        with patch.object(SENSOR.requests, "get", return_value=Mock(content=self.feed.SerializeToString())):
            positions, trips, occupancy = self.data._get_vehicle_positions()
        self.assertEqual(trips, {"trip-1": "V1", "trip-2": "V2"})
        self.assertEqual(set(positions), {"V2"})
        self.assertEqual(occupancy, {"V2": "EMPTY"})

    def test_realtime_time_uses_ha_timezone_not_process_timezone(self):
        self.add_trip("valid")
        with patch.object(fixture.dt_mod, "now", return_value=self.now.astimezone(ZoneInfo("Asia/Tokyo"))):
            details = self.refresh()
            attrs = SENSOR.departure_attributes(details[0])
        self.assertEqual(attrs["due_at"], "21:05")
        self.assertEqual(attrs["due_in"], 5)
        self.assertEqual(details[0].arrival_time.tzinfo, UTC)

    def test_dst_fold_preserves_elapsed_minutes(self):
        now = dt.datetime(2026, 10, 25, 0, 50, tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
        departure = dt.datetime(2026, 10, 25, 1, 10, tzinfo=UTC)
        with patch.object(fixture.dt_mod, "now", return_value=now):
            self.assertEqual(SENSOR.due_in_minutes(departure), 20)
            self.assertEqual(SENSOR.local_time_string(departure), "02:10")

    def test_expired_predictions_disappear_between_feed_refreshes(self):
        self.add_trip("valid")
        self.refresh()
        sensor = SENSOR.PublicTransportSensor(self.data, "S", "R", "Example", "example")
        self.assertEqual(sensor.state, 5)
        with patch.object(fixture.dt_mod, "now", return_value=self.now + dt.timedelta(minutes=6)):
            self.assertIsNone(sensor.state)
            self.assertEqual(sensor.extra_state_attributes[SENSOR.ATTR_UPCOMING_DEPARTURES], [])

    def test_download_error_does_not_expose_credential_url(self):
        with patch.object(SENSOR.requests, "get", side_effect=RuntimeError("https://example.org/?key=private")):
            with self.assertLogs(SENSOR._LOGGER, level="ERROR") as logs:
                self.data._update_route_statuses({}, {}, {})
        self.assertIn("RuntimeError", self.data.last_trip_update_error)
        self.assertNotIn("private", self.data.last_trip_update_error + str(logs.output))

    def test_one_malformed_provider_row_does_not_remove_healthy_departures(self):
        now = self.now.astimezone(ZoneInfo("Asia/Tokyo"))
        future_ms = int((self.now + dt.timedelta(minutes=3)).timestamp()) * 1000
        rows = [None, {"routeId": "R", "predictedArrivalTime": "bad"},
                {"routeId": "R", "predictedArrivalTime": future_ms, "tripId": "valid"}]
        details = REALTIME.filter_onebusaway_arrivals(rows, "R", now)
        self.assertEqual([item.trip_id for item in details], ["valid"])
        self.assertEqual(SENSOR.due_in_minutes(details[0].arrival_time), 3)
        transit = [{"global_stop_id": "S", "route_short_name": "R", "itineraries": [{"schedule_items": [
            {"departure_time": 10**50}, {"departure_time": future_ms // 1000, "is_real_time": True},
        ]}]}]
        self.assertEqual(len(REALTIME.filter_transit_app_departures(transit, global_stop_id="S", configured_route="R", now=now)), 1)
