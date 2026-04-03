from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_NAME

from .config import normalize_feed_config
from .const import CONF_FEED_ID, DOMAIN


class GTFSRtConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Manage GTFS-Realtime config entries created from YAML imports."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """The integration currently relies on YAML-backed imports."""
        return self.async_abort(reason="yaml_only")

    async def async_step_import(self, import_data):
        """Create or update a config entry from YAML configuration."""
        data = normalize_feed_config(import_data)
        feed_id = data[CONF_FEED_ID]

        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_FEED_ID) != feed_id:
                continue
            if entry.data != data or entry.title != data[CONF_NAME]:
                self.hass.config_entries.async_update_entry(entry, data=data, title=data[CONF_NAME])
                if entry.state is config_entries.ConfigEntryState.LOADED:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        return self.async_create_entry(title=data[CONF_NAME], data=data)
