"""Availability must follow the source covering each sensor, including empty feeds."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from google.transit import gtfs_realtime_pb2 as pb
import pytest

from custom_components.gtfs_rt.sensor import PublicTransportData, PublicTransportSensor

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "primary_ok,transit_ok,has_departure,expected_a,expected_b",
    [
        (False, True, True, False, True),
        (True, False, False, True, True),
        (False, True, False, False, True),
    ],
)
async def test_partial_or_empty_provider_health_is_scoped_to_each_sensor(
    hass, primary_ok, transit_ok, has_departure, expected_a, expected_b
):
    now = datetime.now(timezone.utc)
    empty = pb.FeedMessage()
    empty.header.gtfs_realtime_version = "2.0"
    rows = (
        [
            {
                "global_stop_id": "AG:T",
                "route_short_name": "B",
                "itineraries": [
                    {
                        "schedule_items": [
                            {
                                "departure_time": int(
                                    (now + timedelta(minutes=5)).timestamp()
                                ),
                                "is_real_time": True,
                            }
                        ]
                    }
                ],
            }
        ]
        if has_departure
        else []
    )

    def get(url, **kwargs):
        if url == "https://example.org/trips":
            if not primary_ok:
                raise RuntimeError("synthetic primary failure")
            return Mock(content=empty.SerializeToString())
        if not transit_ok:
            raise RuntimeError("synthetic optional failure")
        return Mock(status_code=200, json=Mock(return_value={"route_departures": rows}))

    data = PublicTransportData(
        "https://example.org/trips",
        monitored_departures=[("A", "S"), ("B", "T"), ("", "T")],
        transit_api_key="synthetic",
        transit_departures=[("B", "T", "AG:T", "B")],
    )
    with patch("custom_components.gtfs_rt.sensor.requests.get", side_effect=get):
        await hass.async_add_executor_job(data.update)
    a = PublicTransportSensor(data, "S", "A", "Missing", "a")
    b = PublicTransportSensor(data, "T", "B", "Covered", "b")
    wide = PublicTransportSensor(data, "T", "", "All routes", "wide")
    assert wide.available is primary_ok
    assert a.available is expected_a
    assert b.available is expected_b
    assert bool(a.extra_state_attributes["Problem reason"]) is (not expected_a)
    if not has_departure:
        assert b.state is None
    if not primary_ok and has_departure:
        with patch(
            "custom_components.gtfs_rt.sensor.dt_util.now",
            return_value=now + timedelta(minutes=6),
        ):
            assert not b.available
