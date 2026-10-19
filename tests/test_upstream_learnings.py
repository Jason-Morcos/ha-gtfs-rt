"""Regressions for upstream v1.0.0 learnings using the real protobuf bindings."""

import datetime as dt
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from google.protobuf import json_format
from google.transit import gtfs_realtime_pb2 as pb
import test_sensor as fixture

SENSOR = fixture.sensor_module
FEED = sys.modules["custom_components.gtfs_rt.feed"]
UTC = dt.timezone.utc


class UpstreamFeedTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 4, 3, 12, tzinfo=UTC)
        self.clock = patch.object(fixture.dt_mod, "now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.data = SENSOR.PublicTransportData("https://example.org/secret?key=private")
        self.trips = pb.FeedMessage()
        self.trips.header.gtfs_realtime_version = "2.0"

    def add_trip(self, trip_id="t", route="R", start_time="", start_date=""):
        row = self.trips.entity.add(id=str(len(self.trips.entity))).trip_update
        row.trip.trip_id, row.trip.route_id = trip_id, route
        if start_time:
            row.trip.start_time = start_time
        if start_date:
            row.trip.start_date = start_date
        row.stop_time_update.add(stop_id="S").departure.time = (
            int(self.now.timestamp()) + 300
        )
        return row

    def refresh(self, content=None, maps=None):
        content = self.trips.SerializeToString() if content is None else content
        with patch.object(SENSOR.requests, "get", return_value=Mock(content=content)):
            self.data._update_route_statuses(*(maps or ({}, {}, {})))
        return self.data.info

    def test_json_camel_snake_unknown_fields_and_binary_header_brace(self):
        self.add_trip()
        for snake in (False, True):
            encoded = json_format.MessageToJson(
                self.trips, preserving_proto_field_name=snake
            )
            encoded = encoded[:-1] + ', "futureField": true}'
            self.assertEqual(len(self.refresh(encoded.encode())["R"]["S"]), 1)
        # Binary starts with 0x0a and a header length of 123 (a literal brace).
        self.trips.header.gtfs_realtime_version = "x" * 121
        binary = self.trips.SerializeToString()
        self.assertTrue(binary.startswith(b"\n{"))
        self.assertEqual(FEED.parse_feed(binary), self.trips)

    def test_bad_payloads_fail_closed_with_safe_actionable_messages(self):
        for payload, hint in [
            (b"", "empty"),
            (b"PKsecret", "ZIP"),
            (b"<html>secret", "HTML"),
            (b"debug secret", "text"),
            (b'{"error":"secret"}', "JSON"),
        ]:
            self.refresh(payload)
            self.assertIn(hint, self.data.last_trip_update_error)
            self.assertNotIn("secret", self.data.last_trip_update_error)
            self.assertEqual(self.data.info, {})
        empty = pb.FeedMessage()
        empty.header.gtfs_realtime_version = "2.0"
        self.assertEqual(FEED.parse_feed(empty.SerializeToString()), empty)

    def test_accept_preserves_case_insensitive_user_override_and_auth_is_safe(self):
        self.add_trip()
        for headers, expected in [
            ({}, FEED.ACCEPT),
            ({"accept": "custom/type"}, "custom/type"),
        ]:
            with patch.object(
                SENSOR.requests,
                "get",
                return_value=Mock(content=self.trips.SerializeToString()),
            ) as get:
                FEED.fetch_feed("https://example.org/private", headers)
            sent = get.call_args.kwargs["headers"]
            self.assertEqual(
                [value for key, value in sent.items() if key.lower() == "accept"],
                [expected],
            )
        response = Mock(status_code=401)
        response.raise_for_status.side_effect = RuntimeError("private URL")
        with patch.object(SENSOR.requests, "get", return_value=response):
            with self.assertRaisesRegex(FEED.FeedAuthError, "HTTP 401") as caught:
                FEED.fetch_feed("https://example.org/private", {})
        self.assertNotIn("private", str(caught.exception))

    def test_frequency_instances_survive_dedup_and_get_their_own_vehicles(self):
        self.add_trip("f", start_time="08:00:00")
        self.add_trip("f", start_time="08:10:00")
        self.add_trip("f", start_time="08:20:00")
        vehicles = pb.FeedMessage()
        for index, start in enumerate(("08:00:00", "08:10:00")):
            row = vehicles.entity.add(id=str(index)).vehicle
            row.trip.trip_id, row.trip.start_time = "f", start
            row.vehicle.id = str(index)
            row.position.latitude, row.position.longitude = index + 1, index + 2
        mapping = FEED.VehicleIndex(vehicles)
        details = self.refresh(maps=({}, mapping, {}))["R"]["S"]
        self.assertEqual(len(details), 3)
        self.assertEqual(
            [d.position.latitude if d.position else None for d in details], [1, 2, None]
        )
        self.assertNotIn("f", mapping)

    def test_frequency_service_dates_and_conflicting_single_vehicle(self):
        a = self.add_trip("f", start_time="08:00:00", start_date="20260403")
        b = self.add_trip("f", start_time="08:00:00", start_date="20260404")
        vehicles = pb.FeedMessage()
        row = vehicles.entity.add(id="v").vehicle
        row.trip.CopyFrom(a.trip)
        row.vehicle.id = "v"
        row.position.latitude, row.position.longitude = 1, 2
        mapping = FEED.VehicleIndex(vehicles)
        self.assertIsNotNone(mapping.for_trip(a.trip))
        self.assertIsNone(mapping.for_trip(b.trip))
        details = self.refresh(maps=({"v": row.position}, mapping, {}))["R"]["S"]
        self.assertEqual(len(details), 2)
        self.assertIsNotNone(details[0].position)
        self.assertIsNone(details[1].position)

    def test_route_recovery_only_from_matching_trip_including_idless_vehicle(self):
        trip = self.add_trip(route="")
        vehicles = pb.FeedMessage()
        row = vehicles.entity.add(id="v").vehicle
        row.trip.trip_id, row.trip.route_id = "t", "R"
        self.assertIn("R", self.refresh(maps=({}, FEED.VehicleIndex(vehicles), {})))
        row.trip.trip_id = "previous-trip"
        row.vehicle.id = trip.vehicle.id = "bus"
        self.assertNotIn("R", self.refresh(maps=({}, FEED.VehicleIndex(vehicles), {})))

    def test_vehicle_outage_does_not_break_trips_and_recovery_clears_status(self):
        self.add_trip()
        self.data._vehicle_position_url = "https://example.org/vehicles"

        def get(url, **kwargs):
            if url.endswith("vehicles"):
                raise RuntimeError("private-url")
            return Mock(content=self.trips.SerializeToString())

        with patch.object(SENSOR.requests, "get", side_effect=get):
            self.data.update()
        self.assertEqual(len(self.data.info["R"]["S"]), 1)
        self.assertIsNone(self.data.last_trip_update_error)
        self.assertEqual(self.data.vehicle_feed_error, "RuntimeError")
        with patch.object(
            SENSOR.requests,
            "get",
            return_value=Mock(content=self.trips.SerializeToString()),
        ):
            self.data._get_vehicle_positions()
        self.assertIsNone(self.data.vehicle_feed_error)

    def test_deleted_relationship_bad_timestamp_and_sequence_only_stop_are_ignored(
        self,
    ):
        bad = self.add_trip("deleted")
        bad.trip.schedule_relationship = pb.TripDescriptor.DELETED
        bad = self.add_trip("bad-time")
        bad.stop_time_update[0].departure.time = 2**62
        bad = self.add_trip("no-stop-id")
        bad.stop_time_update[0].ClearField("stop_id")
        self.add_trip("good")
        result = self.refresh()
        self.assertEqual(list(result["R"]), ["S"])
        self.assertEqual([d.trip_id for d in result["R"]["S"]], ["good"])

    def test_stop_wide_sensor_collects_routes_and_exposes_ten_departures(self):
        for i in range(12):
            trip = self.add_trip(str(i), route="R" if i % 2 else "Q")
            trip.stop_time_update[0].departure.time += i * 60
        self.refresh()
        sensor = SENSOR.PublicTransportSensor(self.data, "S", "", "All routes", "all")
        self.assertEqual(
            len(sensor.extra_state_attributes[SENSOR.ATTR_UPCOMING_DEPARTURES]), 10
        )
        self.assertEqual(
            {
                d["route_id"]
                for d in sensor.extra_state_attributes[SENSOR.ATTR_UPCOMING_DEPARTURES]
            },
            {"Q", "R"},
        )


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_redacts_credentials_from_every_config_surface(self):
        spec = importlib.util.spec_from_file_location(
            "custom_components.gtfs_rt.diagnostics",
            Path(__file__).resolve().parents[1]
            / "custom_components/gtfs_rt/diagnostics.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        secret = "SENSITIVE_SENTINEL"
        url = f"https://user:{secret}@example.org/{secret}?key={secret}#{secret}"
        entry = types.SimpleNamespace(
            entry_id="e",
            data={
                "trip_update_url": url,
                "vehicle_position_url": url,
                "static_schedule_url": url,
                "stop_arrivals_url_template": url,
                "headers": {"Authorization": secret},
                "transit_api_key": secret,
                "name": secret,
                "departures": [{"stopid": "S", "route": "R", "name": secret}],
            },
        )
        data = types.SimpleNamespace(
            info={"R": {"S": [object()]}},
            last_trip_update_error=None,
            vehicle_feed_error=None,
        )
        result = await module.async_get_config_entry_diagnostics(
            types.SimpleNamespace(data={"gtfs_rt": {"e": data}}), entry
        )
        self.assertNotIn(secret, repr(result))
        self.assertEqual(result["departures"][0]["routes_at_stop"], ["R"])
        self.assertEqual(module.redact_url("https://[invalid"), module.REDACTED)
        result = await module.async_get_config_entry_diagnostics(
            types.SimpleNamespace(data={}), entry
        )
        self.assertFalse(result["runtime"]["loaded"])


class StopWideFallbackTests(unittest.TestCase):
    def test_oba_and_transit_omit_route_filter_without_collapsing_other_routes(self):
        now = dt.datetime(2026, 4, 3, 12, tzinfo=UTC)
        epoch = int(now.timestamp()) + 300
        oba = [
            {"routeId": route, "predictedArrivalTime": epoch * 1000}
            for route in ("R", "Q")
        ]
        self.assertEqual(
            len(fixture.realtime_module.filter_onebusaway_arrivals(oba, "", now)), 2
        )
        transit = [
            {
                "global_stop_id": "S",
                "route_short_name": route,
                "itineraries": [
                    {
                        "schedule_items": [
                            {"departure_time": epoch, "is_real_time": True}
                        ]
                    }
                ],
            }
            for route in ("R", "Q")
        ]
        details = fixture.realtime_module.filter_transit_app_departures(
            transit, global_stop_id="S", configured_route="", now=now
        )
        self.assertEqual(len(details), 2)
        self.assertEqual({detail.route_id for detail in details}, {"R", "Q"})

    def test_stop_wide_schedule_combines_service_and_protects_device(self):
        import test_health
        import test_config

        case = test_health.StaticScheduleValidatorTests()
        case.setUp()
        validator = case.validator
        # Parse with an all-route monitor, as production does for a route-free stop.
        validator._monitored_routes.add("")
        now = dt.datetime(2026, 4, 3, 14, tzinfo=UTC)
        status = validator.get_status("", "23895", now)
        self.assertEqual(status.status, test_health.STATUS_SERVICE_EXPECTED)
        self.assertEqual(status.next_scheduled_departure.hour, 14)
        self.assertFalse(
            test_config.config_module.can_remove_route_device(
                {"feed_id": "f", "departures": [{"stopid": "S", "route": ""}]},
                {("gtfs_rt", "f:stop:S")},
            )
        )
        self.assertTrue(
            test_config.config_module.can_remove_route_device(
                {"feed_id": "f", "departures": []}, {("gtfs_rt", "f:stop:S")}
            )
        )

    def test_optional_route_normalization_preserves_existing_id_algorithm(self):
        import test_config

        config = {
            "trip_update_url": "https://example.org/trips",
            "entity_namespace": "stable",
            "departures": [{"name": "Stop", "stopid": "S"}],
        }
        normalized = test_config.config_module.normalize_feed_config(config)
        self.assertEqual(normalized["departures"][0]["route"], "")
        self.assertEqual(
            normalized, test_config.config_module.normalize_feed_config(normalized)
        )


class CrossProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.at = dt.datetime(2026, 4, 3, 12, tzinfo=UTC)
        self.cls = fixture.realtime_module.StopDetails
        self.combine = fixture.realtime_module.combine_duplicate_departures

    def detail(self, source, **kwargs):
        return self.cls(self.at, None, None, None, tracking_source=source, **kwargs)

    def test_compatible_partial_instance_and_prefixed_route_still_merge(self):
        gtfs = self.detail(
            "gtfs_rt",
            trip_id="trip",
            route_id="R",
            trip_start_date="20260403",
            trip_start_time="08:00:00",
        )
        oba = self.detail("onebusaway", trip_id="1_trip", route_id="1_R")
        transit = self.detail("transit_app", trip_id="trip", route_id="Route R")
        result = self.combine([gtfs, oba, transit])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].tracking_source, "transit_app")

    def test_unknown_instance_cannot_be_guessed_between_frequency_runs(self):
        early = self.detail("gtfs_rt", trip_id="trip", trip_start_time="08:00:00")
        later = self.detail("gtfs_rt", trip_id="trip", trip_start_time="08:10:00")
        unknown = self.detail("onebusaway", trip_id="1_trip")
        self.assertEqual(len(self.combine([early, later, unknown])), 3)
        same_run = self.detail(
            "transit_app", trip_id="trip", trip_start_time="08:10:00"
        )
        self.assertEqual(len(self.combine([early, later, same_run])), 2)

    def test_time_only_matching_uses_route_context(self):
        internal = self.detail("gtfs_rt", route_id="internal-R")
        public = self.detail("transit_app", route_id="R")
        self.assertEqual(len(self.combine([internal, public])), 1)
        self.assertEqual(len(self.combine([internal, public], match_routes=True)), 2)


class StopWideMergeTests(unittest.TestCase):
    def test_provider_cache_merge_does_not_combine_different_routes(self):
        at = dt.datetime(2026, 4, 3, 12, tzinfo=UTC)
        first = fixture.realtime_module.StopDetails(
            at, None, None, None, tracking_source="onebusaway", route_id="R"
        )
        second = fixture.realtime_module.StopDetails(
            at, None, None, None, tracking_source="transit_app", route_id="Q"
        )
        data = SENSOR.PublicTransportData("https://example.org/trips")
        data.info = {"": {"S": [first]}}
        data._merge_departure_times({"": {"S": [second]}})
        self.assertEqual(len(data.info[""]["S"]), 2)
        cached = data._future_departure_times(data.info, at - dt.timedelta(minutes=1))
        self.assertEqual(len(cached[""]["S"]), 2)


class AgencyIdentityTests(unittest.TestCase):
    def test_stop_wide_prefixes_do_not_collapse_distinct_agency_trips(self):
        at = dt.datetime(2026, 4, 3, 12, tzinfo=UTC)
        detail = fixture.realtime_module.StopDetails
        combine = fixture.realtime_module.combine_duplicate_departures
        a = detail(at, None, None, None, trip_id="1_trip", route_id="R")
        b = detail(at, None, None, None, trip_id="2_trip", route_id="Q")
        self.assertEqual(len(combine([a, b], match_routes=True)), 2)
        # Even if routes coincide, same-provider raw IDs remain distinct.
        b = detail(at, None, None, None, trip_id="2_trip", route_id="R")
        self.assertEqual(len(combine([a, b])), 2)
        b = detail(
            at,
            None,
            None,
            None,
            trip_id="2_trip",
            route_id="R",
            tracking_source="onebusaway",
        )
        self.assertEqual(len(combine([a, b])), 2)
