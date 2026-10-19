from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
import math

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
    occupancy: str | int | None
    delay: int | None
    tracking_source: str = TRACKING_SOURCE_GTFS_RT
    is_realtime: bool = True
    trip_id: str | None = None
    scheduled_time: dt.datetime | None = None
    trip_start_date: str | None = None
    trip_start_time: str | None = None
    route_id: str | None = None
    # Stop sequences are provider-specific: OBA indices are not GTFS sequences.
    _stop_sequence: tuple[str, int] | None = field(default=None, repr=False)
    # Keep original reports when a preferred provider omits service-instance
    # metadata. Later cache/stop-wide merges must retain the same evidence.
    _reports: tuple[StopDetails, ...] = field(default=(), compare=False, repr=False)


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


def _shares_trip_id(detail, group, ambiguities, match_routes=False):
    canonical_id = _canonical_trip_id(detail.trip_id)
    if not canonical_id:
        return False
    candidates = [
        item for item in group if _canonical_trip_id(item.trip_id) == canonical_id
    ]
    if not candidates:
        return False
    ambiguous_instances, ambiguous_aliases, ambiguous_visits = ambiguities
    instance = (detail.trip_start_date, detail.trip_start_time)
    for candidate in candidates:
        # Every report in a group must agree. A third provider's unprefixed ID
        # cannot bridge two distinct IDs from one feed or two known agencies.
        if detail.tracking_source == candidate.tracking_source:
            if detail.trip_id != candidate.trip_id:
                return False
            if match_routes and detail.route_id != candidate.route_id:
                return False
        if match_routes and detail.route_id and candidate.route_id and not (
            route_id_matches(detail.route_id, candidate.route_id)
            or route_id_matches(candidate.route_id, detail.route_id)
        ):
            return False
        if has_numeric_prefix(detail.trip_id) and has_numeric_prefix(
            candidate.trip_id
        ):
            if (
                str(detail.trip_id).partition("_")[0]
                != str(candidate.trip_id).partition("_")[0]
            ):
                return False
        visit, other_visit = detail._stop_sequence, candidate._stop_sequence
        if visit and other_visit and visit[0] == other_visit[0] and visit != other_visit:
            return False
        # An unindexed provider cannot bridge separate visits on a loop trip.
        for namespace in ambiguous_visits.get(canonical_id, ()):
            if not (visit and other_visit and visit == other_visit and visit[0] == namespace):
                return False
        other = (candidate.trip_start_date, candidate.trip_start_time)
        if any(
            left and right and left != right for left, right in zip(instance, other)
        ):
            return False
        if canonical_id in ambiguous_instances and instance != other:
            return False
        if canonical_id in ambiguous_aliases and detail.trip_id != candidate.trip_id:
            return False
    return True


def _trip_ambiguities(details):
    dates, times, prefixes, source_ids, visits = {}, {}, {}, {}, {}
    for detail in details:
        canonical = _canonical_trip_id(detail.trip_id)
        if not canonical:
            continue
        if detail._stop_sequence:
            namespace, sequence = detail._stop_sequence
            visits.setdefault((canonical, namespace), set()).add(sequence)
        if detail.trip_start_date:
            dates.setdefault(canonical, set()).add(detail.trip_start_date)
        if detail.trip_start_time:
            times.setdefault(canonical, set()).add(detail.trip_start_time)
        if has_numeric_prefix(detail.trip_id):
            prefixes.setdefault(canonical, set()).add(
                str(detail.trip_id).partition("_")[0]
            )
        source_ids.setdefault((canonical, detail.tracking_source), set()).add(
            detail.trip_id
        )
    instances = {
        key
        for index in (dates, times)
        for key, values in index.items()
        if len(values) > 1
    }
    aliases = {key for key, values in prefixes.items() if len(values) > 1}
    aliases.update(
        key for (key, _source), values in source_ids.items() if len(values) > 1
    )
    ambiguous_visits = {}
    for (canonical, namespace), sequences in visits.items():
        if len(sequences) > 1:
            ambiguous_visits.setdefault(canonical, set()).add(namespace)
    return instances, aliases, ambiguous_visits


def _within_duplicate_window(detail: StopDetails, group: list[StopDetails]) -> bool:
    return all(
        abs(detail.arrival_time - candidate.arrival_time) <= DUPLICATE_DEPARTURE_WINDOW
        for candidate in group
    )


def _within_scheduled_duplicate_window(
    detail: StopDetails, group: list[StopDetails]
) -> bool:
    if detail.scheduled_time is None:
        return False
    known = [
        candidate.scheduled_time
        for candidate in group
        if candidate.scheduled_time is not None
    ]
    return bool(known) and all(
        abs(detail.scheduled_time - when) <= SCHEDULED_DUPLICATE_DEPARTURE_WINDOW
        for when in known
    )


def _has_cross_source_overlap(detail: StopDetails, group: list[StopDetails]) -> bool:
    return any(
        detail.tracking_source != candidate.tracking_source for candidate in group
    )


def _group_has_tracking_source(detail: StopDetails, group: list[StopDetails]) -> bool:
    return any(
        detail.tracking_source == candidate.tracking_source for candidate in group
    )


def _can_merge_by_time(
    detail: StopDetails, group: list[StopDetails], match_routes=False
) -> bool:
    group_trip_ids = _trip_ids(group)
    if _canonical_trip_id(detail.trip_id) or group_trip_ids:
        return False

    # Time proximity alone can collapse genuinely frequent service. Only use it
    # as a fallback when distinct providers report a trip without identifiers.
    if match_routes and any(
        (
            has_numeric_prefix(detail.route_id)
            and has_numeric_prefix(candidate.route_id)
            and str(detail.route_id).partition("_")[0]
            != str(candidate.route_id).partition("_")[0]
        )
        or not detail.route_id
        or not candidate.route_id
        or normalize_prefixed_id(detail.route_id)
        != normalize_prefixed_id(candidate.route_id)
        for candidate in group
    ):
        return False
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


def combine_duplicate_departures(
    details: list[StopDetails], *, match_routes=False
) -> list[StopDetails]:
    """Combine duplicate departures reported by multiple realtime sources."""
    observations = []
    for detail in details:
        for report in detail._reports or (detail,):
            if report not in observations:
                observations.append(report)
    groups: list[list[StopDetails]] = []
    ambiguities = _trip_ambiguities(observations)
    for detail in sorted(observations, key=lambda item: item.arrival_time):
        for group in groups:
            if (
                detail in group
                or _shares_trip_id(detail, group, ambiguities, match_routes)
                or _can_merge_by_time(detail, group, match_routes)
            ):
                group.append(detail)
                break
        else:
            groups.append([detail])

    combined = [
        replace(max(group, key=_departure_quality), _reports=tuple(group))
        for group in groups
    ]
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
    if not configured_route:
        return True
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


def valid_position(latitude, longitude):
    """Accept only an explicitly supplied finite geographic coordinate pair."""
    if (
        latitude is None
        or longitude is None
        or isinstance(latitude, bool)
        or isinstance(longitude, bool)
    ):
        return None
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return RealtimePosition(latitude, longitude)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _occupancy(*values):
    return next(
        (
            value
            for value in values
            if isinstance(value, (str, int))
            and not isinstance(value, bool)
            and value != ""
        ),
        None,
    )


def build_onebusaway_stop_details(item: dict) -> StopDetails | None:
    """Convert an OBA arrival row into StopDetails."""
    predicted_departure = int(item.get("predictedDepartureTime") or 0)
    predicted_ms = predicted_departure or int(item.get("predictedArrivalTime") or 0)
    # A different scheduled event includes dwell time and cannot establish delay
    # or a matching scheduled instant for a realtime prediction.
    if predicted_ms:
        scheduled_field = "scheduledDepartureTime" if predicted_departure else "scheduledArrivalTime"
        scheduled_ms = int(item.get(scheduled_field) or 0)
    else:
        scheduled_ms = int(item.get("scheduledDepartureTime") or item.get("scheduledArrivalTime") or 0)
    chosen_ms = predicted_ms or scheduled_ms
    if chosen_ms <= 0:
        return None

    trip_status = _mapping(item.get("tripStatus"))
    position_data = _mapping(trip_status.get("position")) or _mapping(
        trip_status.get("lastKnownLocation")
    )
    position = valid_position(position_data.get("lat"), position_data.get("lon"))
    occupancy = _occupancy(
        item.get("predictedOccupancy"),
        item.get("occupancyStatus"),
        trip_status.get("occupancyStatus"),
    )
    delay = (
        int((predicted_ms - scheduled_ms) / 1000)
        if predicted_ms and scheduled_ms
        else None
    )
    is_realtime = bool(predicted_ms)

    return StopDetails(
        arrival_time=dt.datetime.fromtimestamp(chosen_ms / 1000, dt.timezone.utc),
        position=position,
        occupancy=occupancy,
        delay=delay,
        tracking_source=TRACKING_SOURCE_ONEBUSAWAY
        if is_realtime
        else TRACKING_SOURCE_SCHEDULE,
        is_realtime=is_realtime,
        trip_id=_string_or_none(item.get("tripId") or trip_status.get("activeTripId")),
        scheduled_time=(
            dt.datetime.fromtimestamp(scheduled_ms / 1000, dt.timezone.utc)
            if scheduled_ms
            else None
        ),
        route_id=_string_or_none(item.get("routeId")),
        _stop_sequence=("onebusaway", item["stopSequence"])
        if type(item.get("stopSequence")) is int and item["stopSequence"] >= 0
        else None,
    )


def filter_onebusaway_arrivals(
    arrivals: list[dict],
    configured_route: str,
    now: dt.datetime,
) -> list[StopDetails]:
    """Select and sort future arrivals for the configured route."""
    matches: list[StopDetails] = []
    for item in _list(arrivals):
        if not isinstance(item, dict):
            continue
        if item.get("departureEnabled") is False:
            continue
        if not route_id_matches(configured_route, item.get("routeId")):
            continue
        try:
            details = build_onebusaway_stop_details(item)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if details is None or details.arrival_time <= now:
            continue
        matches.append(details)
    return combine_duplicate_departures(matches, match_routes=not configured_route)


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

    occupancy = _occupancy(item.get("occupancy"), item.get("occupancy_status"))

    return StopDetails(
        arrival_time=dt.datetime.fromtimestamp(departure_time, dt.timezone.utc),
        position=None,
        occupancy=occupancy,
        delay=delay,
        tracking_source=TRACKING_SOURCE_TRANSIT_APP
        if is_realtime
        else TRACKING_SOURCE_SCHEDULE,
        is_realtime=is_realtime,
        trip_id=_string_or_none(
            item.get("rt_trip_id") or item.get("trip_id") or item.get("trip_search_key")
        ),
        scheduled_time=(
            dt.datetime.fromtimestamp(scheduled_time, dt.timezone.utc)
            if isinstance(scheduled_time, int)
            else None
        ),
    )


def transit_route_matches(configured_route: str, route_entry: dict) -> bool:
    """Match a configured Transit route label against a Transit API route entry."""
    if not configured_route:
        return True
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
    for route_entry in _list(route_departures):
        if not isinstance(route_entry, dict):
            continue
        if str(route_entry.get("global_stop_id") or "") != str(global_stop_id):
            continue
        if not transit_route_matches(configured_route, route_entry):
            continue

        itineraries = [
            *_list(route_entry.get("itineraries")),
            *_list(route_entry.get("merged_itineraries")),
        ]
        for itinerary in itineraries:
            if not isinstance(itinerary, dict):
                continue
            for item in _list(itinerary.get("schedule_items")):
                if not isinstance(item, dict) or item.get("is_cancelled"):
                    continue
                try:
                    details = build_transit_app_stop_details(item)
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
                if details is None or details.arrival_time <= now:
                    continue
                matches.append(
                    replace(
                        details,
                        route_id=_string_or_none(
                            route_entry.get("route_id")
                            or route_entry.get("route_short_name")
                        ),
                    )
                )

    return combine_duplicate_departures(matches, match_routes=not configured_route)
