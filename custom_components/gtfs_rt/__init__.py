from __future__ import annotations

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.const import Platform

from .config import FEED_CONFIG_SCHEMA, normalize_feed_config
from .const import DOMAIN

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

    for raw_feed in config.get(DOMAIN, []):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=normalize_feed_config(dict(raw_feed)),
            )
        )

    return True


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
