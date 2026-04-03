from __future__ import annotations

import asyncio

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform

from .config import FEED_CONFIG_SCHEMA, normalize_feed_config
from .const import CONF_FEED_ID, DOMAIN

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(cv.ensure_list, [vol.Schema(FEED_CONFIG_SCHEMA)]),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Import YAML feed definitions into config entries."""
    hass.data.setdefault(DOMAIN, {})
    import_tasks = []
    configured_feed_ids = set()

    for raw_feed in config.get(DOMAIN, []):
        normalized = normalize_feed_config(dict(raw_feed))
        configured_feed_ids.add(normalized[CONF_FEED_ID])
        import_tasks.append(
            hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=normalized,
            )
            )
        )

    if import_tasks:
        hass.async_create_task(_async_setup_imported_entries(hass, import_tasks, configured_feed_ids))

    return True


async def _async_setup_imported_entries(hass, import_tasks, configured_feed_ids):
    """Finish YAML imports and ensure matching entries are actively set up."""
    await asyncio.gather(*import_tasks)
    await asyncio.sleep(0)

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_FEED_ID) not in configured_feed_ids:
            continue
        if entry.disabled_by or entry.state is not ConfigEntryState.NOT_LOADED:
            continue
        await hass.config_entries.async_setup(entry.entry_id)


async def async_setup_entry(hass, entry):
    """Set up GTFS-Realtime from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    """Unload a GTFS-Realtime config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
