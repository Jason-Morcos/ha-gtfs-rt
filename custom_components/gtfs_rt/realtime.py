from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

TRACKING_SOURCE_GTFS_RT = "gtfs_rt"
TRACKING_SOURCE_ONEBUSAWAY = "onebusaway"
TRACKING_SOURCE_SCHEDULE = "schedule"
TRACKING_SOURCE_TRANSIT_APP = "transit_app"


@dataclass(frozen=True)
class RealtimePosition:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class StopDetails:
    arrival_time: dt.datetime
    position: RealtimePosition | None
    occupancy: str | None
    delay: int | None
    tracking_source: str = TRACKING_SOURCE_GTFS_RT
    is_realtime: bool = True


def normalize_prefixed_id(value: str | None) -> str | None:
    """Strip an agency prefix from ids like `1_100214`."""
    if value is None:
        return None
    text = str(value)
    prefix, separator, remainder = text.partition("_")
    if separator and prefix.isdigit():
        return remainder
    return text


def has_numeric_prefix(value: str | None) -> bool:
    """Return whether an id uses a numeric agency prefix like `1_100214`."""
    if value is None:
        return False
    text = str(value)
    prefix, separator, _remainder = text.partition("_")
    return bool(separator and prefix.isdigit())


def route_id_matches(configured_route: str, observed_route: str | None) -> bool:
    """Match a configured route id against a provider route id."""
    configured = str(configured_route)
    if observed_route is None:
        return False
    observed = str(observed_route)

    if has_numeric_prefix(configured):
        return configured == observed

    normalized_observed = normalize_prefixed_id(observed)
    if normalized_observed is None:
        return False
    return configured == normalized_observed


def build_onebusaway_stop_details(item: dict) -> StopDetails | None:
    """Convert an OBA arrival row into StopDetails."""
    predicted_ms = int(item.get("predictedArrivalTime") or item.get("predictedDepartureTime") or 0)
    scheduled_ms = int(item.get("scheduledArrivalTime") or item.get("scheduledDepartureTime") or 0)
    chosen_ms = predicted_ms or scheduled_ms
    if chosen_ms <= 0:
        return None

    trip_status = item.get("tripStatus") or {}
    position_data = trip_status.get("position") or trip_status.get("lastKnownLocation") or {}
    position = None
    lat = position_data.get("lat")
    lon = position_data.get("lon")
    if lat is not None and lon is not None:
        position = RealtimePosition(latitude=float(lat), longitude=float(lon))

    occupancy = (
        item.get("predictedOccupancy")
        or item.get("occupancyStatus")
        or trip_status.get("occupancyStatus")
        or None
    )
    delay = int((predicted_ms - scheduled_ms) / 1000) if predicted_ms and scheduled_ms else None
    is_realtime = bool(predicted_ms)

    return StopDetails(
        arrival_time=dt.datetime.fromtimestamp(chosen_ms / 1000),
        position=position,
        occupancy=occupancy or None,
        delay=delay,
        tracking_source=TRACKING_SOURCE_ONEBUSAWAY if is_realtime else TRACKING_SOURCE_SCHEDULE,
        is_realtime=is_realtime,
    )


def filter_onebusaway_arrivals(
    arrivals: list[dict],
    configured_route: str,
    now: dt.datetime,
) -> list[StopDetails]:
    """Select and sort future arrivals for the configured route."""
    matches: list[StopDetails] = []
    for item in arrivals:
        if not route_id_matches(configured_route, item.get("routeId")):
            continue
        details = build_onebusaway_stop_details(item)
        if details is None or details.arrival_time <= now:
            continue
        matches.append(details)
    matches.sort(key=lambda item: item.arrival_time)
    return matches


def build_transit_app_stop_details(item: dict) -> StopDetails | None:
    """Convert a Transit app schedule item into StopDetails."""
    departure_time = item.get("departure_time")
    if not isinstance(departure_time, int):
        return None

    is_realtime = bool(item.get("is_real_time"))

    scheduled_time = item.get("scheduled_departure_time") or item.get("scheduled_time")
    delay = None
    if isinstance(scheduled_time, int):
        delay = departure_time - scheduled_time

    occupancy = item.get("occupancy") or item.get("occupancy_status") or None

    return StopDetails(
        arrival_time=dt.datetime.fromtimestamp(departure_time),
        position=None,
        occupancy=occupancy,
        delay=delay,
        tracking_source=TRACKING_SOURCE_TRANSIT_APP if is_realtime else TRACKING_SOURCE_SCHEDULE,
        is_realtime=is_realtime,
    )


def transit_route_matches(configured_route: str, route_entry: dict) -> bool:
    """Match a configured Transit route label against a Transit API route entry."""
    configured = str(configured_route)
    return configured in {
        str(route_entry.get("route_short_name") or ""),
        str(route_entry.get("route_id") or ""),
        str(route_entry.get("global_route_id") or ""),
    }


def filter_transit_app_departures(
    route_departures: list[dict],
    *,
    global_stop_id: str,
    configured_route: str,
    now: dt.datetime,
) -> list[StopDetails]:
    """Select and sort future Transit app departures for a stop/route."""
    matches: list[StopDetails] = []
    for route_entry in route_departures:
        if str(route_entry.get("global_stop_id") or "") != str(global_stop_id):
            continue
        if not transit_route_matches(configured_route, route_entry):
            continue

        for itinerary in route_entry.get("itineraries") or []:
            if not isinstance(itinerary, dict):
                continue
            for item in itinerary.get("schedule_items") or []:
                if not isinstance(item, dict) or item.get("is_cancelled"):
                    continue
                details = build_transit_app_stop_details(item)
                if details is None or details.arrival_time <= now:
                    continue
                matches.append(details)

    matches.sort(key=lambda item: item.arrival_time)
    return matches
