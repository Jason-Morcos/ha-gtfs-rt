from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.const import CONF_NAME, CONF_UNIQUE_ID

from .const import (
    CONF_API_KEY,
    CONF_APIKEY,
    CONF_DEPARTURES,
    CONF_ENTITY_NAMESPACE,
    CONF_FEED_ID,
    CONF_HEADERS,
    CONF_ROUTE,
    CONF_STATIC_SCHEDULE_URL,
    CONF_STOP_ID,
    CONF_STOP_ARRIVALS_URL_TEMPLATE,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_X_API_KEY,
    DEFAULT_NAME,
    DEFAULT_TITLE,
)

DEPARTURE_SCHEMA = {
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Required(CONF_STOP_ID): cv.string,
    vol.Required(CONF_ROUTE): cv.string,
}

FEED_CONFIG_SCHEMA = {
    vol.Optional(CONF_NAME, default=DEFAULT_TITLE): cv.string,
    vol.Optional(CONF_ENTITY_NAMESPACE): cv.string,
    vol.Required(CONF_TRIP_UPDATE_URL): cv.string,
    vol.Exclusive(CONF_API_KEY, "headers"): cv.string,
    vol.Exclusive(CONF_X_API_KEY, "headers"): cv.string,
    vol.Exclusive(CONF_APIKEY, "headers"): cv.string,
    vol.Exclusive(CONF_HEADERS, "headers"): {cv.string: cv.string},
    vol.Optional(CONF_VEHICLE_POSITION_URL): cv.string,
    vol.Optional(CONF_STATIC_SCHEDULE_URL): cv.string,
    vol.Optional(CONF_STOP_ARRIVALS_URL_TEMPLATE): cv.string,
    vol.Required(CONF_DEPARTURES): [DEPARTURE_SCHEMA],
}


def build_headers(config: dict) -> dict[str, str]:
    """Build the outbound HTTP headers for a GTFS feed definition."""
    headers = dict(config.get(CONF_HEADERS, {}))
    if (api_key := config.get(CONF_API_KEY)) is not None:
        headers["Authorization"] = api_key
    elif (apikey := config.get(CONF_APIKEY)) is not None:
        headers["apikey"] = apikey
    elif (x_api_key := config.get(CONF_X_API_KEY)) is not None:
        headers["x-api-key"] = x_api_key
    return headers


def derive_feed_id(config: dict) -> str:
    """Return a stable feed identifier for config-entry and device grouping."""
    if namespace := config.get(CONF_ENTITY_NAMESPACE):
        return namespace

    payload = {
        CONF_TRIP_UPDATE_URL: config.get(CONF_TRIP_UPDATE_URL),
        CONF_VEHICLE_POSITION_URL: config.get(CONF_VEHICLE_POSITION_URL),
        CONF_STATIC_SCHEDULE_URL: config.get(CONF_STATIC_SCHEDULE_URL),
        CONF_STOP_ARRIVALS_URL_TEMPLATE: config.get(CONF_STOP_ARRIVALS_URL_TEMPLATE),
        CONF_HEADERS: config.get(CONF_HEADERS, {}),
    }
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"feed_{digest}"


def derive_feed_title(config: dict) -> str:
    """Build a human-friendly title for the config entry."""
    if name := config.get(CONF_NAME):
        return name
    if namespace := config.get(CONF_ENTITY_NAMESPACE):
        return namespace
    parsed = urlparse(config[CONF_TRIP_UPDATE_URL])
    if parsed.netloc:
        return parsed.netloc
    return DEFAULT_TITLE


def derive_departure_unique_id(feed_id: str, departure: dict) -> str:
    """Generate a stable entity unique_id when the user has not set one."""
    payload = {
        CONF_NAME: departure.get(CONF_NAME, DEFAULT_NAME),
        CONF_ROUTE: departure[CONF_ROUTE],
        CONF_STOP_ID: departure[CONF_STOP_ID],
        CONF_FEED_ID: feed_id,
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_feed_config(config: dict) -> dict:
    """Normalize YAML or config-flow input into stored config-entry data."""
    headers = build_headers(config)
    feed_id = config.get(CONF_FEED_ID) or derive_feed_id({**config, CONF_HEADERS: headers})

    departures = []
    for departure in config[CONF_DEPARTURES]:
        departure_dict = {
            CONF_NAME: departure.get(CONF_NAME, DEFAULT_NAME),
            CONF_ROUTE: str(departure[CONF_ROUTE]),
            CONF_STOP_ID: str(departure[CONF_STOP_ID]),
        }
        departure_dict[CONF_UNIQUE_ID] = str(
            departure.get(CONF_UNIQUE_ID) or derive_departure_unique_id(feed_id, departure_dict)
        )
        departures.append(departure_dict)

    normalized = {
        CONF_FEED_ID: feed_id,
        CONF_NAME: derive_feed_title(config),
        CONF_TRIP_UPDATE_URL: config[CONF_TRIP_UPDATE_URL],
        CONF_DEPARTURES: departures,
    }
    if headers:
        normalized[CONF_HEADERS] = headers
    if vehicle_position_url := config.get(CONF_VEHICLE_POSITION_URL):
        normalized[CONF_VEHICLE_POSITION_URL] = vehicle_position_url
    if static_schedule_url := config.get(CONF_STATIC_SCHEDULE_URL):
        normalized[CONF_STATIC_SCHEDULE_URL] = static_schedule_url
    if stop_arrivals_url_template := config.get(CONF_STOP_ARRIVALS_URL_TEMPLATE):
        normalized[CONF_STOP_ARRIVALS_URL_TEMPLATE] = stop_arrivals_url_template
    if entity_namespace := config.get(CONF_ENTITY_NAMESPACE):
        normalized[CONF_ENTITY_NAMESPACE] = entity_namespace
    return normalized
