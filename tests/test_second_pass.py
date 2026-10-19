"""Cross-provider interactions and malformed optional data from the second review."""

import datetime as dt
import importlib.util
import itertools
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from google.transit import gtfs_realtime_pb2 as pb
import test_sensor as fixture

RT, SENSOR = fixture.realtime_module, fixture.sensor_module
NOW = dt.datetime(2026, 4, 3, 12, tzinfo=dt.timezone.utc)


class RepeatedMergeTests(unittest.TestCase):
    def detail(self, source="gtfs_rt", seconds=300, **kwargs):
        return RT.StopDetails(
            NOW + dt.timedelta(seconds=seconds),
            None,
            None,
            None,
            tracking_source=source,
            **kwargs,
        )

    def test_unprefixed_provider_cannot_bridge_known_agencies_in_any_order(self):
        reports = [
            self.detail(trip_id="1_trip", route_id="R"),
            self.detail(trip_id="2_trip", route_id="Q"),
            self.detail("transit_app", trip_id="trip", route_id="R"),
        ]
        for ordering in itertools.permutations(reports):
            self.assertEqual(
                len(RT.combine_duplicate_departures(list(ordering), match_routes=True)),
                3,
            )
            initial = RT.combine_duplicate_departures(
                list(ordering[:2]), match_routes=True
            )
            self.assertEqual(
                len(
                    RT.combine_duplicate_departures(
                        initial + [ordering[2]], match_routes=True
                    )
                ),
                3,
            )

    def test_later_frequency_evidence_can_split_an_earlier_partial_match(self):
        early = self.detail(trip_id="f", trip_start_time="08:00:00")
        late = self.detail(trip_id="f", trip_start_time="08:10:00")
        preferred = self.detail("transit_app", trip_id="f")
        initial = RT.combine_duplicate_departures([early, preferred])
        self.assertEqual(len(initial), 1)
        self.assertEqual(initial[0].tracking_source, "transit_app")
        self.assertEqual(len(RT.combine_duplicate_departures(initial + [late])), 3)
        self.assertEqual(RT.combine_duplicate_departures(initial), initial)
        with patch.object(fixture.dt_mod, "now", return_value=NOW):
            self.assertNotIn("_reports", SENSOR.departure_attributes(initial[0]))

    def test_time_only_group_cannot_chain_past_its_duplicate_window(self):
        reports = [
            self.detail("gtfs_rt", 300),
            self.detail("onebusaway", 419),
            self.detail("transit_app", 538),
        ]
        self.assertEqual(len(RT.combine_duplicate_departures(reports)), 2)
        same_minute = [
            self.detail("gtfs_rt", 300),
            self.detail("onebusaway", 340),
            self.detail("transit_app", 360),
        ]
        self.assertEqual(len(RT.combine_duplicate_departures(same_minute)), 1)

    def test_time_only_stop_wide_matching_respects_known_agency_prefixes(self):
        reports = [
            self.detail("gtfs_rt", route_id="1_R"),
            self.detail("onebusaway", route_id="2_R"),
        ]
        self.assertEqual(
            len(RT.combine_duplicate_departures(reports, match_routes=True)), 2
        )


class MalformedProviderTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(fixture.dt_mod, "now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.epoch = int((NOW + dt.timedelta(minutes=5)).timestamp())

    def test_bad_optional_oba_metadata_cannot_drop_good_predictions(self):
        rows = [
            {
                "routeId": "R",
                "predictedArrivalTime": self.epoch * 1000,
                "tripStatus": value,
                "predictedOccupancy": 0,
                "tripId": f"t{i}",
            }
            for i, value in enumerate(("bad", [], 4, {"position": "bad"}))
        ]
        result = RT.filter_onebusaway_arrivals(rows, "R", NOW)
        self.assertEqual(len(result), 4)
        self.assertTrue(all(detail.position is None for detail in result))
        self.assertTrue(all(detail.occupancy == 0 for detail in result))

    def test_nested_transit_collections_are_isolated_from_healthy_neighbors(self):
        good = {
            "global_stop_id": "S",
            "route_short_name": "R",
            "itineraries": [
                {
                    "schedule_items": [
                        {
                            "departure_time": self.epoch,
                            "is_real_time": True,
                            "occupancy": 0,
                        }
                    ]
                }
            ],
        }
        malformed = [
            {**good, "itineraries": value}
            for value in (
                4,
                "bad",
                {},
                [{"schedule_items": 4}],
                [{"schedule_items": "bad"}],
            )
        ]
        result = RT.filter_transit_app_departures(
            malformed + [good], global_stop_id="S", configured_route="R", now=NOW
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].occupancy, 0)

    def test_oba_invalid_envelopes_take_the_configured_gtfs_fallback(self):
        for payload in (
            [],
            {},
            {"data": []},
            {"data": {"entry": []}},
            {"data": {"entry": {"arrivalsAndDepartures": "bad"}}},
        ):
            data = SENSOR.PublicTransportData(
                "https://example.org/trips",
                monitored_departures=[("R", "S")],
                stop_arrivals_url_template="https://example.org/{stop_id}",
            )
            calls = []

            def fallback(*args):
                calls.append("gtfs")
                data.info = {
                    "R": {
                        "S": [
                            RT.StopDetails(
                                NOW + dt.timedelta(minutes=5), None, None, None
                            )
                        ]
                    }
                }

            data._update_route_statuses = fallback
            with patch.object(
                SENSOR.requests,
                "get",
                return_value=Mock(status_code=200, json=Mock(return_value=payload)),
            ):
                data.update()
            self.assertEqual(calls, ["gtfs"])
            self.assertIsNone(data.get_departure_error("R", "S"))

    def test_invalid_transit_envelope_keeps_the_existing_primary(self):
        data = SENSOR.PublicTransportData(
            "https://example.org/trips",
            transit_api_key="synthetic",
            transit_departures=[("R", "S", "AG:S", "R")],
        )
        original = RT.StopDetails(NOW + dt.timedelta(minutes=5), None, None, None)
        data.info = {"R": {"S": [original]}}
        for payload in ([], {}, {"route_departures": None}, {"route_departures": {}}):
            with patch.object(
                SENSOR.requests,
                "get",
                return_value=Mock(status_code=200, json=Mock(return_value=payload)),
            ):
                error = data._update_transit_app_statuses()
            self.assertIn("ValueError", error)
            self.assertEqual(data.info["R"]["S"], [original])

    def test_invalid_oba_coordinates_only_remove_enrichment(self):
        for lat, lon in (
            ("nan", 2),
            (1, "inf"),
            (91, 2),
            (1, 181),
            (True, 2),
            (1, None),
        ):
            row = {
                "routeId": "R",
                "predictedArrivalTime": self.epoch * 1000,
                "tripStatus": {"position": {"lat": lat, "lon": lon}},
            }
            details = RT.filter_onebusaway_arrivals([row], "R", NOW)
            self.assertEqual(len(details), 1)
            self.assertIsNone(details[0].position)
        self.assertEqual(RT.valid_position(0, 0), RT.RealtimePosition(0, 0))

    def test_partial_or_invalid_protobuf_position_does_not_invent_coordinates(self):
        for lat, lon in ((1, None), (float("nan"), 2), (100, 2)):
            feed = pb.FeedMessage()
            feed.header.gtfs_realtime_version = "2.0"
            row = feed.entity.add(id="v").vehicle
            row.vehicle.id, row.trip.trip_id = "v", "t"
            row.position.latitude = lat
            if lon is not None:
                row.position.longitude = lon
            data = SENSOR.PublicTransportData(
                "https://example.org/trips",
                vehicle_position_url="https://example.org/vehicles",
            )
            with patch.object(
                SENSOR.requests,
                "get",
                return_value=Mock(content=feed.SerializePartialToString()),
            ):
                positions, trips, occupancy = data._get_vehicle_positions()
            self.assertEqual(positions, {})
            if lon is None:
                # A partially populated Position violates required protobuf
                # fields; reject that sidecar feed without inventing a vehicle.
                self.assertEqual(trips, {})
                self.assertIsNotNone(data.vehicle_feed_error)
            else:
                self.assertIsNone(trips.for_trip(row.trip).position)

    def test_partial_stop_cache_preserves_failed_pairs_during_backoff(self):
        data = SENSOR.PublicTransportData(
            "https://example.org/trips",
            monitored_departures=[("R", "S"), ("Q", "T")],
            stop_arrivals_url_template="https://example.org/{stop_id}",
        )
        data._last_stop_arrival_info = {
            "R": {
                "S": [RT.StopDetails(NOW + dt.timedelta(minutes=5), None, None, None)]
            }
        }
        data._stop_arrivals_backoff_until = NOW + dt.timedelta(minutes=2)
        with patch.object(
            SENSOR.requests,
            "get",
            side_effect=AssertionError("backoff must retain request pacing"),
        ):
            data.update()
        self.assertIsNone(data.get_departure_error("R", "S"))
        self.assertIn("rate limited", data.get_departure_error("Q", "T"))


class StopWideDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_are_observed_inside_stop_wide_buckets_and_counts_deduplicate(
        self,
    ):
        spec = importlib.util.spec_from_file_location(
            "custom_components.gtfs_rt.diagnostics",
            Path(__file__).resolve().parents[1]
            / "custom_components/gtfs_rt/diagnostics.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        detail = RT.StopDetails(NOW, None, None, None, route_id="R")
        data = types.SimpleNamespace(
            info={"": {"S": [detail]}, "R": {"S": [detail]}},
            last_trip_update_error=None,
            vehicle_feed_error=None,
        )
        entry = types.SimpleNamespace(
            entry_id="e", data={"departures": [{"route": "", "stopid": "S"}]}
        )
        diagnostics = await module.async_get_config_entry_diagnostics(
            types.SimpleNamespace(data={"gtfs_rt": {"e": data}}), entry
        )
        self.assertEqual(diagnostics["departures"][0]["routes_at_stop"], ["R"])
        self.assertEqual(diagnostics["departures"][0]["departures_at_stop"], 1)
        self.assertEqual(diagnostics["runtime"]["routes_with_departures"], 1)


class ReviewInteractionRegressions(unittest.TestCase):
    def test_reexpanded_expired_reports_cannot_return_to_sensor_or_cache(self):
        expired = RT.StopDetails(
            NOW - dt.timedelta(seconds=30),
            None,
            None,
            None,
            trip_id="f",
            trip_start_time="08:00:00",
            route_id="R",
        )
        future = RT.StopDetails(
            NOW + dt.timedelta(minutes=2),
            None,
            None,
            None,
            trip_id="f",
            tracking_source="transit_app",
            route_id="R",
        )
        another = RT.StopDetails(
            NOW + dt.timedelta(minutes=3),
            None,
            None,
            None,
            trip_id="f",
            trip_start_time="08:10:00",
        )
        merged = RT.combine_duplicate_departures([expired, future])
        data = SENSOR.PublicTransportData("https://example.org/trips")
        data.info = {"R": {"S": merged}, "": {"S": [another]}}
        sensor = SENSOR.PublicTransportSensor(data, "S", "", "Any route", "any")
        with patch.object(fixture.dt_mod, "now", return_value=NOW):
            live = sensor._get_next_buses()
        self.assertEqual(len(live), 2)
        self.assertTrue(all(detail.arrival_time > NOW for detail in live))
        cached = data._future_departure_times({"R": {"S": merged + [another]}}, NOW)
        self.assertTrue(all(detail.arrival_time > NOW for detail in cached["R"]["S"]))

    def test_api_error_retains_an_earlier_success_when_gtfs_fallback_fails(self):
        data = SENSOR.PublicTransportData(
            "https://example.org/trips",
            monitored_departures=[("R", "S"), ("Q", "T")],
            stop_arrivals_url_template="https://example.org/stop/{stop_id}",
        )
        success = {
            "code": 200,
            "data": {
                "entry": {
                    "arrivalsAndDepartures": [
                        {
                            "routeId": "R",
                            "predictedArrivalTime": int(
                                (NOW + dt.timedelta(minutes=5)).timestamp()
                            )
                            * 1000,
                        }
                    ]
                }
            },
        }
        responses = [
            Mock(status_code=200, json=Mock(return_value=success)),
            Mock(status_code=200, json=Mock(return_value={"code": 500})),
            RuntimeError("synthetic trip failure"),
        ]
        with (
            patch.object(fixture.dt_mod, "now", return_value=NOW),
            patch.object(SENSOR.requests, "get", side_effect=responses),
        ):
            data.update()
            self.assertIsNone(data.get_departure_error("R", "S"))
            self.assertIn("API code 500", data.get_departure_error("Q", "T"))
        self.assertEqual(len(data._last_stop_arrival_info["R"]["S"]), 1)
