"""Verify native flow, entity registration and reload contracts on supported HA."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from google.transit import gtfs_realtime_pb2 as pb
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.gtfs_rt.config import normalize_feed_config
from custom_components.gtfs_rt.diagnostics import async_get_config_entry_diagnostics

pytestmark = pytest.mark.asyncio
URL = "https://example.org/trips"


def feed():
    message = pb.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    row = message.entity.add(id="t").trip_update
    row.trip.trip_id, row.trip.route_id = "t", "R"
    row.stop_time_update.add(stop_id="S").departure.time = int(
        (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()
    )
    return message


async def test_native_ui_flow_options_and_reconfigure_keep_ids(hass):
    with (
        patch("custom_components.gtfs_rt.config_flow.fetch_feed", return_value=feed()),
        patch("custom_components.gtfs_rt.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Example", "trip_update_url": URL}
        )
        assert result["step_id"] == "departure"
        # Exercise the actual form schema, including omitted optional route.
        result["data_schema"]({"name": "Stop", "stopid": "S"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Stop", "stopid": "S"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        entry = result["result"]
        old_id = entry.data["feed_id"]
        old_departure = entry.data["departures"][0]
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_departure"}
        )
        assert result["step_id"] == "add_departure"
        with patch.object(hass.config_entries, "async_schedule_reload"):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"name": "Other", "stopid": "B", "route": "R"}
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.data["departures"][0] == old_departure
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt",
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        with patch.object(hass.config_entries, "async_schedule_reload"):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"name": "Renamed", "trip_update_url": URL + "?key=rotated"},
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data["feed_id"] == old_id
        assert entry.data["departures"][0] == old_departure


async def test_native_entry_entity_device_diagnostics_and_unload(hass):
    data = normalize_feed_config(
        {
            "name": "Example",
            "entity_namespace": "stable",
            "trip_update_url": URL,
            "departures": [
                {
                    "name": "Route stop",
                    "route": "R",
                    "stopid": "S",
                    "unique_id": "existing-id",
                },
                {"name": "Any route", "stopid": "S", "unique_id": "all-routes"},
            ],
        }
    )
    entry = MockConfigEntry(domain="gtfs_rt", title="Example", data=data)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gtfs_rt.feed.requests.get",
        return_value=Mock(content=feed().SerializeToString()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", "gtfs_rt", "existing-id")
    assert entity_id
    state = hass.states.get(entity_id)
    assert state.state not in {"unknown", "unavailable"}
    assert state.attributes["Upcoming departures"][0]["route_id"] == "R"
    devices = dr.async_get(hass)
    assert any(
        ("gtfs_rt", "stable:route:R") in device.identifiers
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
    )
    assert any(
        ("gtfs_rt", "stable:stop:S") in device.identifiers
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["runtime"]["loaded"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.entry_id not in hass.data["gtfs_rt"]


async def test_yaml_import_identity_and_ui_guard(hass):
    data = normalize_feed_config(
        {
            "name": "Imported",
            "entity_namespace": "yaml-stable",
            "trip_update_url": URL,
            "departures": [
                {"name": "Route", "route": "R", "stopid": "S", "unique_id": "yaml-id"}
            ],
        }
    )
    with patch("custom_components.gtfs_rt.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_IMPORT}, data=data
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        entry = result["result"]
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "yaml_managed"
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt",
            context={"source": config_entries.SOURCE_IMPORT},
            data={**data, "name": "Updated"},
        )
        assert result["type"] is FlowResultType.ABORT
        assert entry.data["feed_id"] == "yaml-stable"
        assert entry.data["departures"][0]["unique_id"] == "yaml-id"


async def test_legacy_yaml_platform_keeps_entity_id(hass):
    from homeassistant.setup import async_setup_component

    config = {
        "sensor": [
            {
                "platform": "gtfs_rt",
                "trip_update_url": URL,
                "departures": [
                    {
                        "name": "Legacy stop",
                        "route": "R",
                        "stopid": "S",
                        "unique_id": "legacy-id",
                    }
                ],
            }
        ]
    }
    with patch(
        "custom_components.gtfs_rt.feed.requests.get",
        return_value=Mock(content=feed().SerializeToString()),
    ):
        assert await async_setup_component(hass, "sensor", config)
        await hass.async_block_till_done()
    state = hass.states.get("sensor.legacy_stop")
    assert state and state.state not in {"unknown", "unavailable"}
    assert (
        er.async_get(hass).async_get_entity_id("sensor", "gtfs_rt", "legacy-id")
        == "sensor.legacy_stop"
    )


async def test_simultaneous_flows_reserve_url_and_recheck_before_creation(hass):
    with (
        patch("custom_components.gtfs_rt.config_flow.fetch_feed", return_value=feed()),
        patch("custom_components.gtfs_rt.async_setup_entry", return_value=True),
    ):
        first = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_USER}
        )
        first = await hass.config_entries.flow.async_configure(
            first["flow_id"], {"name": "First", "trip_update_url": URL}
        )
        assert first["step_id"] == "departure"
        second = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_USER}
        )
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"],
            {"name": "Second", "trip_update_url": URL, "headers": {"key": "other"}},
        )
        assert second["type"] is FlowResultType.ABORT
        assert second["reason"] == "already_in_progress"
        # A YAML import can arrive after validation, so also check at finalization.
        entry = MockConfigEntry(
            domain="gtfs_rt",
            title="Concurrent YAML",
            data={"trip_update_url": URL, "departures": []},
        )
        entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_configure(
            first["flow_id"], {"name": "Stop", "stopid": "S"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert len(hass.config_entries.async_entries("gtfs_rt")) == 1


async def test_departure_ids_are_unique_across_feeds_and_legacy_entities(hass):
    other = MockConfigEntry(
        domain="gtfs_rt",
        title="Other",
        data=normalize_feed_config(
            {
                "name": "Other",
                "trip_update_url": URL + "/other",
                "departures": [
                    {"name": "Other", "stopid": "B", "unique_id": "commute"}
                ],
            }
        ),
    )
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "gtfs_rt", "legacy-id", suggested_object_id="legacy"
    )
    with (
        patch("custom_components.gtfs_rt.config_flow.fetch_feed", return_value=feed()),
        patch("custom_components.gtfs_rt.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "New", "trip_update_url": URL}
        )
        for identity in ("commute", "legacy-id"):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"name": "New stop", "stopid": "S", "unique_id": identity},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"] == {"base": "duplicate_departure"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "New stop", "stopid": "S", "unique_id": "unused"},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        entry = result["result"]
        options = await hass.config_entries.options.async_init(entry.entry_id)
        options = await hass.config_entries.options.async_configure(
            options["flow_id"], {"next_step_id": "add_departure"}
        )
        options = await hass.config_entries.options.async_configure(
            options["flow_id"],
            {"name": "Collision", "stopid": "C", "unique_id": "commute"},
        )
        assert options["type"] is FlowResultType.FORM
        assert options["errors"] == {"base": "duplicate_departure"}


async def test_rotating_url_frees_old_url_without_reusing_feed_identity(hass):
    with (
        patch("custom_components.gtfs_rt.config_flow.fetch_feed", return_value=feed()),
        patch("custom_components.gtfs_rt.async_setup_entry", return_value=True),
    ):
        first = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_USER}
        )
        first = await hass.config_entries.flow.async_configure(
            first["flow_id"], {"name": "First", "trip_update_url": URL}
        )
        first = await hass.config_entries.flow.async_configure(
            first["flow_id"], {"name": "Stop", "route": "R", "stopid": "S"}
        )
        entry = first["result"]
        original_feed = entry.data["feed_id"]
        original_departure = entry.data["departures"][0]
        change = await hass.config_entries.flow.async_init(
            "gtfs_rt",
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        with patch.object(hass.config_entries, "async_schedule_reload"):
            change = await hass.config_entries.flow.async_configure(
                change["flow_id"], {"name": "Changed", "trip_update_url": URL + "/new"}
            )
        assert change["reason"] == "reconfigure_successful"
        assert entry.data["feed_id"] == original_feed
        assert entry.data["departures"][0] == original_departure
        second = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_USER}
        )
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"], {"name": "Second", "trip_update_url": URL}
        )
        assert second["type"] is FlowResultType.FORM
        assert second["step_id"] == "departure"
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"], {"name": "Stop", "route": "R", "stopid": "S"}
        )
        assert second["type"] is FlowResultType.CREATE_ENTRY
        assert second["data"]["feed_id"] != original_feed
        assert (
            second["data"]["departures"][0]["unique_id"]
            != original_departure["unique_id"]
        )


async def test_native_ui_rejects_headerless_feed_before_creating_entry(hass):
    malformed = feed()
    malformed.ClearField("header")
    with patch("custom_components.gtfs_rt.feed.requests.get", return_value=Mock(content=malformed.SerializePartialToString())):
        result = await hass.config_entries.flow.async_init(
            "gtfs_rt", context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Example", "trip_update_url": URL}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"trip_update_url": "invalid_feed"}
    assert not hass.config_entries.async_entries("gtfs_rt")
