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


def _observed_routes(info):
    routes = set()
    for route, stops in list(info.items()):
        for details in list(stops.values()):
            for detail in details:
                for report in getattr(detail, "_reports", ()) or (detail,):
                    observed = getattr(report, "route_id", None) or route
                    if observed:
                        routes.add(observed)
    return routes


def _stop_details(info, stop_id):
    rows = []
    routes = set()
    for route, stops in list(info.items()):
        for detail in stops.get(stop_id, []):
            if detail not in rows:
                rows.append(detail)
            for report in getattr(detail, "_reports", ()) or (detail,):
                observed = getattr(report, "route_id", None) or route
                if observed:
                    routes.add(observed)
    return rows, sorted(routes)


async def async_get_config_entry_diagnostics(hass, entry):
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    info = data.info if data else {}
    departures = []
    for departure in entry.data.get(CONF_DEPARTURES, []):
        stop_id, route_id = departure[CONF_STOP_ID], departure.get(CONF_ROUTE) or ""
        details, routes = _stop_details(info, stop_id)
        get_error = getattr(data, "get_departure_error", None)
        departures.append(
            {
                "stop_id": stop_id,
                "route_id": route_id or None,
                "routes_at_stop": routes,
                "departures_at_stop": len(details),
                "error": get_error(route_id, stop_id) if get_error else None,
            }
        )
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
            "primary_feed_error": getattr(data, "_primary_trip_update_error", None),
            "vehicle_feed_error": data.vehicle_feed_error if data else None,
            "transit_feed_error": getattr(data, "transit_feed_error", None),
            "routes_with_departures": len(_observed_routes(info)),
        },
        "departures": departures,
    }
