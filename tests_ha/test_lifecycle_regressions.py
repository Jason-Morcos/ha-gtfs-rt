"""Exercise authoritative forms and YAML ownership through native HA flows."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from google.transit import gtfs_realtime_pb2 as pb
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.gtfs_rt.config import normalize_feed_config
from custom_components.gtfs_rt.feed import ACCEPT

pytestmark = pytest.mark.asyncio
URL = "https://example.org/trips"


def feed_data(namespace="yaml-one", url=URL, unique_id="departure-one"):
    return normalize_feed_config(
        {
            "name": "Example",
            "entity_namespace": namespace,
            "trip_update_url": url,
            "departures": [
                {
                    "name": "Route stop",
                    "route": "R",
                    "stopid": "S",
                    "unique_id": unique_id,
                }
            ],
        }
    )


async def import_feed(hass, data):
    return await hass.config_entries.flow.async_init(
        "gtfs_rt", context={"source": config_entries.SOURCE_IMPORT}, data=data
    )


async def test_yaml_import_cannot_duplicate_ui_url(hass):
    original = feed_data("ui-owned")
    entry = MockConfigEntry(
        domain="gtfs_rt", data=original, source=config_entries.SOURCE_USER
    )
    entry.add_to_hass(hass)
    result = await import_feed(hass, feed_data("yaml-owned", unique_id="yaml-id"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == original
    assert len(hass.config_entries.async_entries("gtfs_rt")) == 1


async def test_yaml_import_cannot_adopt_ui_feed_id(hass):
    original = feed_data("ui-owned")
    entry = MockConfigEntry(
        domain="gtfs_rt", data=original, source=config_entries.SOURCE_USER
    )
    entry.add_to_hass(hass)
    result = await import_feed(hass, feed_data("ui-owned", URL + "/other", "yaml-id"))
    assert result["reason"] == "already_configured"
    assert entry.data == original
    assert entry.source == config_entries.SOURCE_USER
    assert len(hass.config_entries.async_entries("gtfs_rt")) == 1


@pytest.mark.parametrize("updating", [False, True])
async def test_yaml_import_rejects_unique_id_owned_by_another_entry(hass, updating):
    other = MockConfigEntry(
        domain="gtfs_rt",
        data=feed_data("other", URL + "/other", "occupied"),
        source=config_entries.SOURCE_IMPORT,
    )
    other.add_to_hass(hass)
    original = feed_data()
    if updating:
        own = MockConfigEntry(
            domain="gtfs_rt", data=original, source=config_entries.SOURCE_IMPORT
        )
        own.add_to_hass(hass)
    result = await import_feed(hass, feed_data(unique_id="occupied"))
    assert result["reason"] == "duplicate_departure"
    assert len(hass.config_entries.async_entries("gtfs_rt")) == (2 if updating else 1)
    if updating:
        assert own.data == original


async def test_yaml_import_migrates_its_legacy_registry_identity(hass):
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "sensor", "gtfs_rt", "legacy-id", suggested_object_id="saved_commute"
    )
    with patch(
        "custom_components.gtfs_rt.feed.requests.get", return_value=realtime_response()
    ):
        result = await import_feed(hass, feed_data(unique_id="legacy-id"))
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    current = registry.async_get(legacy.entity_id)
    assert current.config_entry_id == result["result"].entry_id
    assert (
        registry.async_get_entity_id("sensor", "gtfs_rt", "legacy-id")
        == legacy.entity_id
    )
    assert hass.states.get(legacy.entity_id).state not in {"unknown", "unavailable"}


async def test_distinct_yaml_namespaces_can_share_url_and_update_own_id(hass):
    with patch("custom_components.gtfs_rt.async_setup_entry", return_value=True):
        first = await import_feed(hass, feed_data())
        second = await import_feed(
            hass, feed_data("yaml-two", unique_id="departure-two")
        )
        assert first["type"] is second["type"] is FlowResultType.CREATE_ENTRY
        er.async_get(hass).async_get_or_create(
            "sensor", "gtfs_rt", "departure-one", config_entry=first["result"]
        )
        updated = {**feed_data(), "name": "Renamed"}
        result = await import_feed(hass, updated)
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert first["result"].data == updated
    assert first["result"].data["feed_id"] == "yaml-one"
    assert first["result"].data["departures"][0]["unique_id"] == "departure-one"
    assert len(hass.config_entries.async_entries("gtfs_rt")) == 2


def realtime_response():
    message = pb.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    trip = message.entity.add(id="trip").trip_update
    trip.trip.trip_id, trip.trip.route_id = "trip", "R"
    trip.stop_time_update.add(stop_id="S").departure.time = int(
        (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()
    )
    return Mock(content=message.SerializeToString())


@pytest.mark.parametrize("explicit_empty", [False, True])
async def test_reconfigure_optional_fields_are_cleared_before_real_reload(
    hass, explicit_empty
):
    data = {
        **feed_data("ui-owned"),
        "vehicle_position_url": "https://example.org/old-vehicles",
        "static_schedule_url": "https://example.org/static",
        "stop_arrivals_url_template": "https://example.org/arrivals/{stop_id}",
        "headers": {"x-api-key": "old-key"},
        "transit_api_key": "old-transit-key",
        "outside_form": "preserved",
    }
    entry = MockConfigEntry(
        domain="gtfs_rt", title="Example", data=data, source=config_entries.SOURCE_USER
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gtfs_rt.feed.requests.get", return_value=realtime_response()
    ) as get:
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt",
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        # HA's optional string widget omits cleared values from JSON. Also
        # accept API callers which submit explicit empty values instead.
        submitted = {"name": "Example", "trip_update_url": URL}
        if explicit_empty:
            submitted.update(
                {
                    "vehicle_position_url": "",
                    "static_schedule_url": "",
                    "stop_arrivals_url_template": "",
                    "headers": {},
                    "transit_api_key": "",
                }
            )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], submitted
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.state is config_entries.ConfigEntryState.LOADED
    for optional in (
        "vehicle_position_url",
        "static_schedule_url",
        "stop_arrivals_url_template",
        "headers",
        "transit_api_key",
    ):
        assert optional not in entry.data
    assert entry.data["outside_form"] == "preserved"
    assert entry.data["feed_id"] == data["feed_id"]
    assert entry.data["departures"] == data["departures"]
    assert get.call_count == 2  # Validation followed by the actual reload refresh.
    assert all(
        call.args[0] == URL and call.kwargs["headers"] == {"Accept": ACCEPT}
        for call in get.call_args_list
    )


async def test_loaded_reconfigure_reloads_new_urls_headers_and_keeps_entities(hass):
    data = {
        **feed_data("stable"),
        "vehicle_position_url": "https://example.org/old-vehicles",
        "headers": {"x-api-key": "old-key"},
    }
    entry = MockConfigEntry(
        domain="gtfs_rt", title="Example", data=data, source=config_entries.SOURCE_USER
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gtfs_rt.feed.requests.get", return_value=realtime_response()
    ) as get:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("sensor", "gtfs_rt", "departure-one")
        old_runtime = hass.data["gtfs_rt"][entry.entry_id]
        devices = dr.async_get(hass)
        old_devices = {
            device.id
            for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        }
        assert entity_id and old_devices
        get.reset_mock()
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt",
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        new_urls = {URL + "/new", "https://example.org/new-vehicles"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "name": "Renamed",
                "trip_update_url": URL + "/new",
                "vehicle_position_url": "https://example.org/new-vehicles",
                "headers": {"x-api-key": "new-key"},
            },
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert hass.data["gtfs_rt"][entry.entry_id] is not old_runtime
    assert (
        registry.async_get_entity_id("sensor", "gtfs_rt", "departure-one") == entity_id
    )
    assert hass.states.get(entity_id).state not in {"unknown", "unavailable"}
    assert {
        device.id
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
    } == old_devices
    assert entry.data["feed_id"] == "stable"
    assert entry.data["departures"] == data["departures"]
    assert get.call_count == 4  # Both URLs validated, then fetched by the new runtime.
    assert {call.args[0] for call in get.call_args_list} == new_urls
    assert all(
        call.kwargs["headers"] == {"x-api-key": "new-key", "Accept": ACCEPT}
        for call in get.call_args_list
    )
