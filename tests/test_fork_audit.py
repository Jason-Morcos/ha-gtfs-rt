"""Synthetic regressions from the broader fork audit."""

import datetime as dt
import itertools
import unittest
from unittest.mock import Mock, patch

from google.transit import gtfs_realtime_pb2 as pb
import test_sensor as fixture

RT, SENSOR = fixture.realtime_module, fixture.sensor_module
NOW = dt.datetime(2026, 4, 3, 12, tzinfo=dt.timezone.utc)


def response(payload, status=200):
    return Mock(
        status_code=status,
        headers={"Retry-After": "120"},
        json=Mock(return_value=payload),
    )


def oba_payload(rows):
    return {"code": 200, "data": {"entry": {"arrivalsAndDepartures": rows}}}


class ForkAuditTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(fixture.dt_mod, "now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def test_failed_first_stop_does_not_starve_other_stops_during_warmup(self):
        for failure in (RuntimeError("offline"), response({"code": 404})):
            data = SENSOR.PublicTransportData(
                "https://example.org/trips",
                monitored_departures=[("R", "bad"), ("Q", "good")],
                stop_arrivals_url_template="https://example.org/{stop_id}",
            )
            success = response(
                oba_payload(
                    [
                        {
                            "routeId": "Q",
                            "predictedDepartureTime": int(
                                (NOW + dt.timedelta(minutes=5)).timestamp() * 1000
                            ),
                        }
                    ]
                )
            )
            with patch.object(
                SENSOR.requests, "get", side_effect=[failure, success]
            ) as get:
                error = data._update_stop_arrival_statuses()
            self.assertEqual(get.call_count, 2)
            self.assertIsNotNone(error)
            self.assertEqual(len(data.info["Q"]["good"]), 1)
            self.assertEqual(data._stop_arrivals_last_refresh["good"], NOW)
            self.assertNotIn("bad", data._stop_arrivals_last_refresh)
            self.assertEqual(data._last_stop_arrival_info, data.info)

    def test_oba_boarding_uses_departure_time_and_matching_departure_delay(self):
        ms = lambda seconds: int(
            (NOW + dt.timedelta(seconds=seconds)).timestamp() * 1000
        )
        row = {
            "routeId": "R",
            "predictedArrivalTime": ms(-60),
            "predictedDepartureTime": ms(180),
            "scheduledArrivalTime": ms(-120),
            "scheduledDepartureTime": ms(60),
        }
        details = RT.filter_onebusaway_arrivals([row], "R", NOW)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].arrival_time, NOW + dt.timedelta(minutes=3))
        self.assertEqual(details[0].delay, 120)
        row["predictedDepartureTime"], row["predictedArrivalTime"] = ms(-1), ms(60)
        self.assertEqual(RT.filter_onebusaway_arrivals([row], "R", NOW), [])

    def test_arrival_only_oba_prediction_uses_arrival_delay(self):
        ms = lambda seconds: int(
            (NOW + dt.timedelta(seconds=seconds)).timestamp() * 1000
        )
        details = RT.build_onebusaway_stop_details(
            {
                "predictedArrivalTime": ms(300),
                "predictedDepartureTime": 0,
                "scheduledArrivalTime": ms(240),
                "scheduledDepartureTime": ms(420),
            }
        )
        self.assertEqual(details.delay, 60)
        self.assertEqual(details.scheduled_time, NOW + dt.timedelta(minutes=4))

    def test_sequence_namespaces_do_not_confuse_oba_indices_with_gtfs_sequences(self):
        reports = [
            RT.StopDetails(
                NOW + dt.timedelta(minutes=5),
                None,
                None,
                None,
                tracking_source=source,
                trip_id="loop",
                _stop_sequence=(source, sequence),
            )
            for source, sequence in (("onebusaway", 0), ("gtfs_rt", 10))
        ]
        self.assertEqual(len(RT.combine_duplicate_departures(reports)), 1)

    def test_oba_explicit_dropoff_only_is_not_a_boardable_departure(self):
        row = {
            "routeId": "R",
            "predictedDepartureTime": int(
                (NOW + dt.timedelta(minutes=3)).timestamp() * 1000
            ),
        }
        self.assertEqual(
            RT.filter_onebusaway_arrivals(
                [{**row, "departureEnabled": False}], "R", NOW
            ),
            [],
        )
        self.assertEqual(len(RT.filter_onebusaway_arrivals([row], "R", NOW)), 1)

    def test_gtfs_loop_visits_survive_sensor_and_cache_merges(self):
        feed = pb.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        update = feed.entity.add(id="loop").trip_update
        update.trip.trip_id, update.trip.route_id = "loop", "R"
        for sequence, minutes in ((2, 5), (8, 30)):
            stop = update.stop_time_update.add(stop_id="S", stop_sequence=sequence)
            stop.departure.time = int((NOW + dt.timedelta(minutes=minutes)).timestamp())
        data = SENSOR.PublicTransportData("https://example.org/trips")
        with patch.object(SENSOR, "fetch_feed", return_value=feed):
            data._update_route_statuses({}, {}, {})
        sensor = SENSOR.PublicTransportSensor(data, "S", "R", "Loop", "loop")
        self.assertEqual(len(sensor._get_next_buses()), 2)
        self.assertEqual(len(data._future_departure_times(data.info, NOW)["R"]["S"]), 2)

    def test_oba_loop_visits_remain_distinct_and_unknown_provider_cannot_bridge(self):
        rows = [
            {
                "routeId": "R",
                "tripId": "loop",
                "stopSequence": seq,
                "predictedDepartureTime": int(
                    (NOW + dt.timedelta(minutes=minutes)).timestamp() * 1000
                ),
            }
            for seq, minutes in ((0, 5), (6, 30))
        ]
        details = RT.filter_onebusaway_arrivals(rows, "R", NOW)
        self.assertEqual(len(details), 2)
        unknown = RT.StopDetails(
            NOW + dt.timedelta(minutes=5),
            None,
            None,
            None,
            trip_id="loop",
            tracking_source="transit_app",
        )
        for ordering in itertools.permutations(details + [unknown]):
            self.assertEqual(len(RT.combine_duplicate_departures(list(ordering))), 3)

    def test_transit_batch_failure_retains_fresh_success_and_failed_batch_cache(self):
        for failure in (RuntimeError("offline"), response({}, 429)):
            mappings = [("R", str(i), f"AG:{i:03d}", "R") for i in range(101)]
            data = SENSOR.PublicTransportData(
                "https://example.org/trips",
                transit_api_key="synthetic",
                transit_departures=mappings,
            )
            cached = RT.StopDetails(NOW + dt.timedelta(minutes=10), None, None, None)
            data._last_transit_app_info = {"R": {"100": [cached]}}
            success = response(
                {
                    "route_departures": [
                        {
                            "global_stop_id": "AG:000",
                            "route_id": "R",
                            "itineraries": [
                                {
                                    "schedule_items": [
                                        {
                                            "departure_time": int(
                                                (
                                                    NOW + dt.timedelta(minutes=5)
                                                ).timestamp()
                                            ),
                                            "is_real_time": True,
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
            )
            with patch.object(SENSOR.requests, "get", side_effect=[success, failure]):
                data._update_transit_app_statuses()
            self.assertEqual(len(data.info["R"]["0"]), 1)
            self.assertEqual(data.info["R"]["100"], [cached])
            self.assertEqual(data._last_transit_app_info, data.info)
            data._primary_trip_update_error = "primary offline"
            self.assertIsNone(
                data.get_departure_error("R", "1")
            )  # fresh empty first batch
            self.assertEqual(data.get_departure_error("R", "99"), None)
            self.assertEqual(
                data.get_departure_error("R", "unmapped"), "primary offline"
            )

    def test_failed_first_transit_batch_does_not_starve_later_empty_success(self):
        mappings = [("R", str(i), f"AG:{i:03d}", "R") for i in range(101)]
        data = SENSOR.PublicTransportData(
            "https://example.org/trips",
            transit_api_key="synthetic",
            transit_departures=mappings,
        )
        data._primary_trip_update_error = "primary offline"
        with patch.object(
            SENSOR.requests,
            "get",
            side_effect=[RuntimeError("offline"), response({"route_departures": []})],
        ):
            error = data._update_transit_app_statuses()
        self.assertIn("unavailable", error)
        self.assertEqual(data.get_departure_error("R", "0"), "primary offline")
        self.assertIsNone(data.get_departure_error("R", "100"))
        self.assertEqual(data.info["R"]["100"], [])

    def test_persistent_failed_stop_leaves_warmup_and_preserves_round_robin_pacing(
        self,
    ):
        data = SENSOR.PublicTransportData(
            "https://example.org/trips",
            monitored_departures=[("R", stop) for stop in ("bad", "a", "b")],
            stop_arrivals_url_template="https://example.org/{stop_id}",
        )
        attempts = []

        def get(url, **kwargs):
            stop = url.rsplit("/", 1)[1]
            attempts.append(stop)
            if stop == "bad":
                raise RuntimeError("persistent synthetic failure")
            return response(oba_payload([]))

        with patch.object(SENSOR.requests, "get", side_effect=get):
            data._update_stop_arrival_statuses()
            self.assertEqual(attempts, ["bad", "a", "b"])
            steady = []
            for minute in range(1, 9):
                attempts.clear()
                with patch.object(
                    fixture.dt_mod,
                    "now",
                    return_value=NOW + dt.timedelta(minutes=minute),
                ):
                    data._update_stop_arrival_statuses()
                self.assertLessEqual(len(attempts), 1)
                steady.extend(attempts)
            self.assertTrue({"bad", "a", "b"}.issubset(steady))
        self.assertNotIn("bad", data._stop_arrivals_last_refresh)
        self.assertGreater(data._stop_arrivals_last_refresh["a"], NOW)
        self.assertGreater(data._stop_arrivals_last_refresh["b"], NOW)

    def test_transit_429_stops_remaining_batches_and_retries_only_after_backoff(self):
        mappings = [("R", str(i), f"AG:{i:03d}", "R") for i in range(201)]
        data = SENSOR.PublicTransportData(
            "https://example.org/trips",
            transit_api_key="synthetic",
            transit_departures=mappings,
        )
        with patch.object(
            SENSOR.requests,
            "get",
            side_effect=[response({"route_departures": []}), response({}, 429)],
        ) as get:
            error = data._update_transit_app_statuses()
            self.assertEqual(get.call_count, 2)
            self.assertIn("rate limited", error)
            self.assertEqual(
                data._transit_app_backoff_until, NOW + dt.timedelta(seconds=120)
            )
            data._update_transit_app_statuses()
            self.assertEqual(get.call_count, 2)
        self.assertEqual(data.info["R"]["0"], [])
        self.assertNotIn("200", data.info["R"])

    def test_stop_wide_trip_id_matches_require_compatible_known_routes(self):
        cases = (("R", "Q", 2), ("R", "1_R", 1), ("1_R", "2_R", 2), ("R", "R", 1))
        for left, right, count in cases:
            reports = [
                RT.StopDetails(
                    NOW + dt.timedelta(minutes=5),
                    None,
                    None,
                    None,
                    tracking_source=source,
                    trip_id="trip",
                    route_id=route,
                )
                for source, route in (("gtfs_rt", left), ("onebusaway", right))
            ]
            for ordering in itertools.permutations(reports):
                with self.subTest(left=left, right=right):
                    self.assertEqual(
                        len(
                            RT.combine_duplicate_departures(
                                list(ordering), match_routes=True
                            )
                        ),
                        count,
                    )
                    first = RT.combine_duplicate_departures(
                        [ordering[0]], match_routes=True
                    )
                    self.assertEqual(
                        len(
                            RT.combine_duplicate_departures(
                                first + [ordering[1]], match_routes=True
                            )
                        ),
                        count,
                    )

    def test_oba_delay_does_not_mix_predicted_and_scheduled_event_types(self):
        ms = lambda seconds: int(
            (NOW + dt.timedelta(seconds=seconds)).timestamp() * 1000
        )
        for predicted, scheduled in (
            ("predictedDepartureTime", "scheduledArrivalTime"),
            ("predictedArrivalTime", "scheduledDepartureTime"),
        ):
            with self.subTest(predicted=predicted):
                details = RT.build_onebusaway_stop_details(
                    {predicted: ms(300), scheduled: ms(240)}
                )
                self.assertEqual(details.arrival_time, NOW + dt.timedelta(minutes=5))
                self.assertIsNone(details.delay)
                self.assertIsNone(details.scheduled_time)
