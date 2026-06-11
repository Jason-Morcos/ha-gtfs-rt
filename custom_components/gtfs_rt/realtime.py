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
    trip_id: str | None = None
    scheduled_time: dt.datetime | None = None


DUPLICATE_DEPARTURE_WINDOW = dt.timedelta(minutes=2)
SCHEDULED_DUPLICATE_DEPARTURE_WINDOW = dt.timedelta(seconds=30)
TRACKING_SOURCE_PRIORITY = {
    TRACKING_SOURCE_SCHEDULE: 0,
    TRACKING_SOURCE_GTFS_RT: 1,
    TRACKING_SOURCE_ONEBUSAWAY: 2,
    TRACKING_SOURCE_TRANSIT_APP: 3,
}


def _string_or_none(value) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _canonical_trip_id(value: str | None) -> str | None:
    return normalize_prefixed_id(value)


def _trip_ids(details: list[StopDetails]) -> set[str]:
    return {
        canonical_id
        for detail in details
        if (canonical_id := _canonical_trip_id(detail.trip_id))
    }


def _shares_trip_id(detail: StopDetails, group: list[StopDetails]) -> bool:
    canonical_id = _canonical_trip_id(detail.trip_id)
    return bool(canonical_id and canonical_id in _trip_ids(group))


def _within_duplicate_window(detail: StopDetails, group: list[StopDetails]) -> bool:
    return any(
        abs(detail.arrival_time - candidate.arrival_time) <= DUPLICATE_DEPARTURE_WINDOW
        for candidate in group
    )


def _within_scheduled_duplicate_window(
    detail: StopDetails, group: list[StopDetails]
) -> bool:
    if detail.scheduled_time is None:
        return False
    return any(
        candidate.scheduled_time is not None
        and abs(detail.scheduled_time - candidate.scheduled_time)
        <= SCHEDULED_DUPLICATE_DEPARTURE_WINDOW
        for candidate in group
    )


def _has_cross_source_overlap(detail: StopDetails, group: list[StopDetails]) -> bool:
    return any(detail.tracking_source != candidate.tracking_source for candidate in group)


def _group_has_tracking_source(detail: StopDetails, group: list[StopDetails]) -> bool:
    return any(detail.tracking_source == candidate.tracking_source for candidate in group)


def _can_merge_by_time(detail: StopDetails, group: list[StopDetails]) -> bool:
    group_trip_ids = _trip_ids(group)
    if _canonical_trip_id(detail.trip_id) or group_trip_ids:
        return False

    # Time proximity alone can collapse genuinely frequent service. Only use it
    # as a fallback when distinct providers report a trip without identifiers.
    if _group_has_tracking_source(detail, group):
        return False
    if not _has_cross_source_overlap(detail, group):
        return False

    if _within_scheduled_duplicate_window(detail, group):
        return True
    return _within_duplicate_window(detail, group)


def _departure_quality(detail: StopDetails):
    return (
        TRACKING_SOURCE_PRIORITY.get(detail.tracking_source, 0),
        int(detail.is_realtime),
        int(detail.position is not None),
        int(detail.occupancy is not None),
        int(detail.delay is not None),
    )


def combine_duplicate_departures(details: list[StopDetails]) -> list[StopDetails]:
    """Combine duplicate departures reported by multiple realtime sources."""
    groups: list[list[StopDetails]] = []
    for detail in sorted(details, key=lambda item: item.arrival_time):
        for group in groups:
            if _shares_trip_id(detail, group) or _can_merge_by_time(detail, group):
                group.append(detail)
                break
        else:
            groups.append([detail])

    combined = [max(group, key=_departure_quality) for group in groups]
    combined.sort(key=lambda item: item.arrival_time)
    return combined


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
        trip_id=_string_or_none(item.get("tripId") or trip_status.get("activeTripId")),
        scheduled_time=(
            dt.datetime.fromtimestamp(scheduled_ms / 1000) if scheduled_ms else None
        ),
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
    return combine_duplicate_departures(matches)


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
        trip_id=_string_or_none(
            item.get("rt_trip_id")
            or item.get("trip_id")
            or item.get("trip_search_key")
        ),
        scheduled_time=(
            dt.datetime.fromtimestamp(scheduled_time)
            if isinstance(scheduled_time, int)
            else None
        ),
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

        itineraries = list(route_entry.get("itineraries") or [])
        itineraries.extend(route_entry.get("merged_itineraries") or [])
        for itinerary in itineraries:
            if not isinstance(itinerary, dict):
                continue
            for item in itinerary.get("schedule_items") or []:
                if not isinstance(item, dict) or item.get("is_cancelled"):
                    continue
                details = build_transit_app_stop_details(item)
                if details is None or details.arrival_time <= now:
                    continue
                matches.append(details)

    return combine_duplicate_departures(matches)
