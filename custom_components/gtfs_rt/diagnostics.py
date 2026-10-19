"""Diagnostics expose explicit safe fields, never stored configuration wholesale."""

from __future__ import annotations

from urllib.parse import urlsplit

from .const import (
    CONF_DEPARTURES,
    CONF_ROUTE,
    CONF_STOP_ID,
    DOMAIN,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_STATIC_SCHEDULE_URL,
    CONF_STOP_ARRIVALS_URL_TEMPLATE,
)

REDACTED = "**REDACTED**"


def redact_url(value):
    """Remove userinfo, path, query and fragment, all of which can hold keys."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}/{REDACTED}"
    except (TypeError, ValueError):
        pass
    return REDACTED


async def async_get_config_entry_diagnostics(hass, entry):
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    info = data.info if data else {}
    return {
        "feed": {
            key: redact_url(entry.data[key])
            for key in (
                CONF_TRIP_UPDATE_URL,
                CONF_VEHICLE_POSITION_URL,
                CONF_STATIC_SCHEDULE_URL,
                CONF_STOP_ARRIVALS_URL_TEMPLATE,
            )
            if entry.data.get(key)
        },
        "runtime": {
            "loaded": data is not None,
            "trip_update_error": data.last_trip_update_error if data else None,
            "vehicle_feed_error": data.vehicle_feed_error if data else None,
            "routes_with_departures": len(info),
        },
        "departures": [
            {
                "stop_id": departure[CONF_STOP_ID],
                "route_id": departure.get(CONF_ROUTE) or None,
                "routes_at_stop": sorted(
                    route
                    for route, stops in list(info.items())
                    if stops.get(departure[CONF_STOP_ID]) and route
                ),
                "departures_at_stop": sum(
                    len(stops.get(departure[CONF_STOP_ID], []))
                    for stops in list(info.values())
                ),
            }
            for departure in entry.data.get(CONF_DEPARTURES, [])
        ],
    }
