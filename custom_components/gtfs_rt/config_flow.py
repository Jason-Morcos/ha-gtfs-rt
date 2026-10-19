from __future__ import annotations

from copy import copy
import hashlib
from urllib.parse import urlparse
from uuid import uuid4

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config import normalize_feed_config
from .const import CONF_FEED_ID, DOMAIN
from .feed import FeedAuthError, FeedFetchError, InvalidFeedError, fetch_feed

_OPTIONAL_URLS = (
    "vehicle_position_url",
    "static_schedule_url",
    "stop_arrivals_url_template",
)
_FEED_FIELDS = (
    CONF_NAME,
    "trip_update_url",
    *_OPTIONAL_URLS,
    "headers",
    "transit_api_key",
)


def _strip_input(values):
    """Normalize user-entered text without rewriting stored entry identities."""
    return {
        key: value.strip() if isinstance(value, str) else value
        for key, value in values.items()
    }


def _feed_schema(values=None):
    fields = {vol.Required(CONF_NAME): str, vol.Required("trip_update_url"): str}
    fields.update(
        {vol.Optional(key): str for key in (*_OPTIONAL_URLS, "transit_api_key")}
    )
    fields[vol.Optional("headers")] = selector.ObjectSelector()
    return _with_suggestions(vol.Schema(fields), values or {})


def _with_suggestions(schema, values):
    # Keep Required/Optional marker types intact.
    fields = {}
    for key, validator in schema.schema.items():
        marker = copy(key)
        if marker.schema in values:
            marker.description = {"suggested_value": values[marker.schema]}
        fields[marker] = validator
    return vol.Schema(fields)


def _departure_schema(values=None, *, repeat=False):
    fields = {
        vol.Required(CONF_NAME): str,
        vol.Required("stopid"): str,
        vol.Optional("route", default=""): str,
        vol.Optional("unique_id"): str,
        vol.Optional("transit_global_stop_id"): str,
        vol.Optional("transit_route"): str,
    }
    if repeat:
        fields[vol.Optional("add_another", default=False)] = bool
    return _with_suggestions(vol.Schema(fields), values or {})


def _departure_errors(departure, departures):
    if (
        not departure.get(CONF_NAME, "").strip()
        or not departure.get("stopid", "").strip()
    ):
        return {"base": "invalid_departure"}
    identity = (departure.get("route", ""), departure["stopid"])
    if any((item.get("route", ""), item["stopid"]) == identity for item in departures):
        return {"base": "duplicate_departure"}
    if departure.get("unique_id") and any(
        item.get("unique_id") == departure["unique_id"] for item in departures
    ):
        return {"base": "duplicate_departure"}
    return {}


def _departure_id_errors(hass, feed, departures, excluded=None):
    """The sensor platform's unique-ID namespace spans every feed and legacy YAML."""
    from homeassistant.helpers import entity_registry as er

    normalized = normalize_feed_config({**feed, "departures": departures})
    identifiers = [item["unique_id"] for item in normalized["departures"]]
    if len(set(identifiers)) != len(identifiers):
        return {"base": "duplicate_departure"}
    occupied = {
        item.get("unique_id")
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != excluded
        for item in entry.data.get("departures", [])
    }
    registry = er.async_get(hass)
    for identifier in identifiers:
        if identifier in occupied:
            return {"base": "duplicate_departure"}
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, identifier)
        if entity_id:
            entity = registry.async_get(entity_id)
            if entity and (excluded is None or entity.config_entry_id != excluded):
                return {"base": "duplicate_departure"}
    return {}


async def _validate_feed(hass, data):
    errors = {}
    for key in ("trip_update_url", *_OPTIONAL_URLS):
        value = data.get(key)
        if not value:
            if key == "trip_update_url":
                errors[key] = "invalid_url"
            continue
        try:
            parsed = urlparse(value)
            valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
            parsed.port
        except (ValueError, TypeError):
            valid = False
        if not valid:
            errors[key] = "invalid_url"
    headers = data.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers.items()
    ):
        errors["headers"] = "invalid_headers"
    if not data.get(CONF_NAME, "").strip():
        errors[CONF_NAME] = "invalid_name"
    if errors:
        return errors
    for key in ("trip_update_url", "vehicle_position_url"):
        if not data.get(key):
            continue
        try:
            await hass.async_add_executor_job(fetch_feed, data[key], headers)
        except FeedAuthError:
            errors[key] = "invalid_auth"
        except InvalidFeedError:
            errors[key] = "invalid_feed"
        except FeedFetchError:
            errors[key] = "cannot_connect"
    return errors


class GTFSRtConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Manage GTFS-Realtime feeds from the UI or YAML imports."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GTFSRtOptionsFlow(config_entry)

    def _duplicate_url(self, data, excluded=None):
        return any(
            entry.entry_id != excluded
            and entry.data.get("trip_update_url") == data["trip_update_url"]
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input = _strip_input(user_input)
            errors = await _validate_feed(self.hass, user_input)
            if not errors and self._duplicate_url(user_input):
                errors["base"] = "already_configured"
            if not errors:
                # Reserve the URL across in-progress flows, independent of headers.
                await self.async_set_unique_id(
                    hashlib.sha256(user_input["trip_update_url"].encode()).hexdigest()
                )
                self._abort_if_unique_id_configured()
                self._feed = {**user_input, CONF_FEED_ID: f"ui_{uuid4().hex}"}
                self._departures = []
                return await self.async_step_departure()
        return self.async_show_form(
            step_id="user", data_schema=_feed_schema(user_input), errors=errors
        )

    async def async_step_departure(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input = _strip_input(user_input)
            departure = {
                key: value for key, value in user_input.items() if key != "add_another"
            }
            departure.setdefault("route", "")
            errors = _departure_errors(departure, self._departures)
            if not errors:
                errors = _departure_id_errors(
                    self.hass, self._feed, [*self._departures, departure]
                )
            if not errors:
                if self._duplicate_url(self._feed):
                    return self.async_abort(reason="already_configured")
                self._departures.append(departure)
                if not user_input.get("add_another"):
                    data = normalize_feed_config(
                        {**self._feed, "departures": self._departures}
                    )
                    return self.async_create_entry(title=data[CONF_NAME], data=data)
                user_input = None
        return self.async_show_form(
            step_id="departure",
            data_schema=_departure_schema(user_input, repeat=True),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        if entry.source == config_entries.SOURCE_IMPORT:
            return self.async_abort(reason="yaml_managed")
        errors = {}
        if user_input is not None:
            user_input = _strip_input(user_input)
            candidate = dict(entry.data)
            candidate.update(
                {key: value for key, value in user_input.items() if key in _FEED_FIELDS}
            )
            for key in (*_OPTIONAL_URLS, "headers", "transit_api_key"):
                if key in user_input and not user_input[key]:
                    candidate.pop(key, None)
            errors = await _validate_feed(self.hass, candidate)
            if not errors and self._duplicate_url(candidate, entry.entry_id):
                errors["base"] = "already_configured"
            if not errors:
                data = normalize_feed_config(candidate)
                self.hass.config_entries.async_update_entry(
                    entry,
                    data=data,
                    title=data[CONF_NAME],
                    unique_id=hashlib.sha256(
                        data["trip_update_url"].encode()
                    ).hexdigest(),
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_feed_schema(user_input or entry.data),
            errors=errors,
        )

    async def async_step_import(self, import_data):
        """Create or update a config entry from YAML configuration."""
        data = normalize_feed_config(import_data)
        feed_id = data[CONF_FEED_ID]

        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_FEED_ID) != feed_id:
                continue
            if entry.data != data or entry.title != data[CONF_NAME]:
                self.hass.config_entries.async_update_entry(
                    entry, data=data, title=data[CONF_NAME]
                )
                if entry.state is config_entries.ConfigEntryState.LOADED:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        return self.async_create_entry(title=data[CONF_NAME], data=data)


class GTFSRtOptionsFlow(config_entries.OptionsFlow):
    """Edit departures without changing existing entity identities."""

    def __init__(self, entry):
        super().__init__()
        self._entry = entry

    def _yaml_guard(self):
        return self._entry.source == config_entries.SOURCE_IMPORT

    async def async_step_init(self, user_input=None):
        if self._yaml_guard():
            return self.async_abort(reason="yaml_managed")
        return self.async_show_menu(
            step_id="init", menu_options=["add_departure", "remove_departure"]
        )

    def _save(self, departures):
        data = normalize_feed_config({**self._entry.data, "departures": departures})
        self.hass.config_entries.async_update_entry(self._entry, data=data)
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
        return self.async_create_entry(title="", data={})

    async def async_step_add_departure(self, user_input=None):
        if self._yaml_guard():
            return self.async_abort(reason="yaml_managed")
        errors = {}
        departures = self._entry.data["departures"]
        if user_input is not None:
            user_input = _strip_input(user_input)
            departure = dict(user_input)
            departure.setdefault("route", "")
            errors = _departure_errors(departure, departures)
            if not errors:
                errors = _departure_id_errors(
                    self.hass,
                    self._entry.data,
                    [*departures, departure],
                    self._entry.entry_id,
                )
            if not errors:
                return self._save([*departures, departure])
        return self.async_show_form(
            step_id="add_departure",
            data_schema=_departure_schema(user_input),
            errors=errors,
        )

    async def async_step_remove_departure(self, user_input=None):
        if self._yaml_guard():
            return self.async_abort(reason="yaml_managed")
        departures = self._entry.data["departures"]
        errors = {}
        if user_input is not None:
            chosen = user_input.get("departure")
            remaining = [item for item in departures if item["unique_id"] != chosen]
            if len(remaining) == len(departures):
                errors["base"] = "invalid_departure"
            elif not remaining:
                errors["base"] = "last_departure"
            else:
                return self._save(remaining)
        options = [
            {
                "value": item["unique_id"],
                "label": f"{item['name']} ({item.get('route', '')}, {item['stopid']})",
            }
            for item in departures
        ]
        schema = vol.Schema(
            {
                vol.Required("departure"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(
            step_id="remove_departure", data_schema=schema, errors=errors
        )
