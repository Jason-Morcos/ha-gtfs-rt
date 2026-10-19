from __future__ import annotations

import datetime
from email.utils import parsedate_to_datetime
import logging
from enum import Enum

import requests

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE, CONF_NAME, CONF_UNIQUE_ID, UnitOfTime
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import Throttle

from .availability import should_mark_entity_unavailable
from .config import FEED_CONFIG_SCHEMA, normalize_feed_config
from .const import (
    CONF_DEPARTURES,
    CONF_FEED_ID,
    CONF_HEADERS,
    CONF_ROUTE,
    CONF_STATIC_SCHEDULE_URL,
    CONF_STOP_ID,
    CONF_STOP_ARRIVALS_URL_TEMPLATE,
    CONF_TRANSIT_API_KEY,
    CONF_TRANSIT_GLOBAL_STOP_ID,
    CONF_TRANSIT_ROUTE,
    TRANSIT_API_STOP_DEPARTURES_URL,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    DEFAULT_NAME,
    DOMAIN,
    ICON,
    REQUEST_TIMEOUT,
    TIME_STR_FORMAT,
)
from .feed import FeedFetchError, VehicleIndex, fetch_feed
from .health import STATUS_SERVICE_EXPECTED, StaticScheduleValidator
from .realtime import (
    TRACKING_SOURCE_GTFS_RT,
    StopDetails,
    valid_position,
    combine_duplicate_departures,
    filter_onebusaway_arrivals,
    filter_transit_app_departures,
)

_LOGGER = logging.getLogger(__name__)

ATTR_STOP_ID = "Stop ID"
ATTR_ROUTE = "Route"
ATTR_DUE_IN = "Due in"
ATTR_DUE_AT = "Due at"
ATTR_DELAYED_BY = "Delayed by"
ATTR_OCCUPANCY = "Occupancy"
ATTR_NEXT_UP = "Next bus"
ATTR_NEXT_UP_DUE_IN = "Next bus due in"
ATTR_NEXT_DELAYED_BY = "Next bus delayed by"
ATTR_NEXT_OCCUPANCY = "Next bus occupancy"
ATTR_NEXT_TRACKING_SOURCE = "Next bus tracking source"
ATTR_UPCOMING_DEPARTURES = "Upcoming departures"
ATTR_SERVICE_STATUS = "Service status"
ATTR_SERVICE_TODAY = "Service today"
ATTR_SERVICE_EXPECTED_NOW = "Service expected now"
ATTR_NEXT_SCHEDULED_DEPARTURE = "Next scheduled departure"
ATTR_PROBLEM_REASON = "Problem reason"
ATTR_TRACKING_SOURCE = "Tracking source"

MIN_TIME_BETWEEN_UPDATES = datetime.timedelta(seconds=60)
MIN_TRANSIT_APP_REFRESH_INTERVAL = datetime.timedelta(seconds=60)
DEFAULT_STOP_ARRIVALS_BACKOFF = datetime.timedelta(minutes=5)
MAX_UPCOMING_DEPARTURES = 10
TRANSIT_API_MAX_DEPARTURES_PER_REQUEST = 10
TRANSIT_API_MAX_STOP_IDS_PER_REQUEST = 100


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(FEED_CONFIG_SCHEMA)


class OccupancyStatus(Enum):
    EMPTY = 0
    MANY_SEATS_AVAILABLE = 1
    FEW_SEATS_AVAILABLE = 2
    STANDING_ROOM_ONLY = 3
    CRUSHED_STANDING_ROOM_ONLY = 4
    FULL = 5
    NOT_ACCEPTING_PASSENGERS = 6
    NO_DATA_AVAILABLE = 7
    NOT_BOARDABLE = 8


def due_in_minutes(timestamp):
    """Get the remaining minutes from now until a given datetime object."""
    diff = timestamp - dt_util.now().astimezone(datetime.timezone.utc)
    return int(diff.total_seconds() / 60)


def local_time_string(timestamp):
    """Render a timezone-aware instant in Home Assistant's configured timezone."""
    return timestamp.astimezone(dt_util.now().tzinfo).strftime(TIME_STR_FORMAT)


def departure_attributes(detail):
    """Serialize a realtime departure into state attributes."""
    attrs = {
        "due_at": local_time_string(detail.arrival_time),
        "due_in": due_in_minutes(detail.arrival_time),
        "delay_minutes": detail.delay / 60.0 if detail.delay is not None else None,
        "occupancy": detail.occupancy,
        "tracking_source": detail.tracking_source,
        "is_realtime": detail.is_realtime,
    }
    if detail.position:
        attrs["latitude"] = detail.position.latitude
        attrs["longitude"] = detail.position.longitude
    if detail.trip_id:
        attrs["trip_id"] = detail.trip_id
    if detail.trip_start_date:
        attrs["trip_start_date"] = detail.trip_start_date
    if detail.trip_start_time:
        attrs["trip_start_time"] = detail.trip_start_time
    if detail.route_id:
        attrs["route_id"] = detail.route_id
    return attrs


def _build_shared_data(config):
    monitored_departures = [
        (departure[CONF_ROUTE], departure[CONF_STOP_ID])
        for departure in config[CONF_DEPARTURES]
    ]
    transit_departures = [
        (
            departure[CONF_ROUTE],
            departure[CONF_STOP_ID],
            departure[CONF_TRANSIT_GLOBAL_STOP_ID],
            departure.get(CONF_TRANSIT_ROUTE, departure[CONF_ROUTE]),
        )
        for departure in config[CONF_DEPARTURES]
        if departure.get(CONF_TRANSIT_GLOBAL_STOP_ID)
    ]
    return PublicTransportData(
        config[CONF_TRIP_UPDATE_URL],
        config.get(CONF_VEHICLE_POSITION_URL),
        config.get(CONF_HEADERS, {}),
        monitored_departures,
        config.get(CONF_STATIC_SCHEDULE_URL),
        config.get(CONF_STOP_ARRIVALS_URL_TEMPLATE),
        config.get(CONF_TRANSIT_API_KEY),
        transit_departures,
    )


def _build_sensors(data, config, config_entry=None):
    return [
        PublicTransportSensor(
            data=data,
            stop=departure[CONF_STOP_ID],
            route=departure[CONF_ROUTE],
            name=departure.get(CONF_NAME, DEFAULT_NAME),
            unique_id=departure.get(CONF_UNIQUE_ID),
            feed_id=config.get(CONF_FEED_ID),
            feed_name=config.get(CONF_NAME),
            config_entry_id=config_entry.entry_id if config_entry else None,
        )
        for departure in config[CONF_DEPARTURES]
    ]


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the legacy YAML sensor platform."""
    normalized = normalize_feed_config(dict(config))
    already_imported = any(
        entry.data.get(CONF_FEED_ID) == normalized[CONF_FEED_ID]
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    if already_imported:
        _LOGGER.debug(
            "Skipping legacy platform setup for %s because a config entry already exists",
            normalized.get(CONF_NAME, DEFAULT_NAME),
        )
        return

    _LOGGER.warning(
        "Legacy sensor platform configuration for gtfs_rt is deprecated. "
        "Move the feed under the top-level gtfs_rt section to enable route devices."
    )
    data = _build_shared_data(normalized)
    add_devices(_build_sensors(data, normalized), False)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up GTFS-Realtime sensors from a config entry."""
    config = dict(config_entry.data)
    data = _build_shared_data(config)
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = data
    async_add_entities(_build_sensors(data, config, config_entry), False)


class PublicTransportSensor(SensorEntity):
    """Implementation of a public transport sensor."""

    def __init__(self, data, stop, route, name, unique_id, feed_id=None, feed_name=None, config_entry_id=None):
        self.data = data
        self._stop = stop
        self._route = route
        self._feed_id = feed_id
        self._feed_name = feed_name
        self._config_entry_id = config_entry_id

        self._attr_name = name
        self._attr_icon = ICON
        self._attr_unique_id = str(unique_id) if unique_id else None
        self._attr_native_unit_of_measurement = UnitOfTime.MINUTES

    async def async_added_to_hass(self):
        """Trigger an immediate refresh after startup without blocking entity setup."""
        self.async_schedule_update_ha_state(True)

    def _get_next_buses(self):
        now = dt_util.now().astimezone(datetime.timezone.utc)
        routes = [self.data.info.get(self._route, {})] if self._route else list(self.data.info.values())
        combined = combine_duplicate_departures([
            detail for stops in routes for detail in stops.get(self._stop, [])
            if detail.arrival_time > now
        ], match_routes=not self._route)
        return [detail for detail in combined if detail.arrival_time > now]

    def _get_schedule_status(self):
        return self.data.get_schedule_status(self._route, self._stop)

    def _get_realtime_error(self):
        get_error = getattr(self.data, "get_departure_error", None)
        return get_error(self._route, self._stop) if get_error else self.data.last_trip_update_error

    def _get_problem_reason(self, schedule_status, next_buses):
        if error := self._get_realtime_error():
            return error
        if schedule_status is None:
            return None
        if schedule_status.problem_reason:
            return schedule_status.problem_reason
        if schedule_status.status == STATUS_SERVICE_EXPECTED and len(next_buses) == 0:
            next_departure = schedule_status.next_scheduled_departure
            if next_departure is not None:
                return (
                    "Scheduled service is expected, but the realtime feed has no "
                    f"matching departures before {next_departure.strftime(TIME_STR_FORMAT)}"
                )
            return "Scheduled service is expected, but the realtime feed has no matching departures"
        return None

    @property
    def device_info(self):
        if not self._feed_id or not self._config_entry_id:
            return None

        if not self._route:
            return DeviceInfo(
                identifiers={(DOMAIN, f"{self._feed_id}:stop:{self._stop}")},
                name=f"{self._feed_name or 'GTFS'} Stop {self._stop}",
                entry_type=DeviceEntryType.SERVICE,
                manufacturer="GTFS-Realtime",
                model="Transit Stop",
            )
        route_label = self.data.get_route_label(self._route) or self._route
        feed_prefix = f"{self._feed_name} " if self._feed_name else ""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._feed_id}:route:{self._route}")},
            name=f"{feed_prefix}Route {route_label}",
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="GTFS-Realtime",
            model="Transit Route",
        )

    @property
    def state(self):
        next_buses = self._get_next_buses()
        return due_in_minutes(next_buses[0].arrival_time) if len(next_buses) > 0 else None

    @property
    def available(self):
        next_buses = self._get_next_buses()
        schedule_status = self._get_schedule_status()
        return not should_mark_entity_unavailable(
            last_trip_update_error=self._get_realtime_error(),
            schedule_status=schedule_status,
            has_realtime_departures=len(next_buses) > 0,
        )

    @property
    def extra_state_attributes(self):
        next_buses = self._get_next_buses()
        schedule_status = self._get_schedule_status()
        attrs = {
            ATTR_DUE_IN: self.state,
            ATTR_DUE_AT: None,
            ATTR_DELAYED_BY: None,
            ATTR_OCCUPANCY: None,
            ATTR_LATITUDE: None,
            ATTR_LONGITUDE: None,
            ATTR_NEXT_UP_DUE_IN: None,
            ATTR_NEXT_UP: None,
            ATTR_NEXT_DELAYED_BY: None,
            ATTR_NEXT_OCCUPANCY: None,
            ATTR_NEXT_TRACKING_SOURCE: None,
            ATTR_UPCOMING_DEPARTURES: [],
            ATTR_STOP_ID: self._stop,
            ATTR_ROUTE: self._route,
            ATTR_TRACKING_SOURCE: None,
            ATTR_SERVICE_STATUS: schedule_status.status if schedule_status else None,
            ATTR_SERVICE_TODAY: schedule_status.service_today if schedule_status else None,
            ATTR_SERVICE_EXPECTED_NOW: schedule_status.service_expected_now if schedule_status else None,
            ATTR_NEXT_SCHEDULED_DEPARTURE: (
                schedule_status.next_scheduled_departure.strftime(TIME_STR_FORMAT)
                if schedule_status and schedule_status.next_scheduled_departure
                else None
            ),
            ATTR_PROBLEM_REASON: self._get_problem_reason(schedule_status, next_buses),
        }
        if len(next_buses) > 0:
            attrs[ATTR_DUE_AT] = local_time_string(next_buses[0].arrival_time)
            attrs[ATTR_OCCUPANCY] = next_buses[0].occupancy
            attrs[ATTR_DELAYED_BY] = next_buses[0].delay / 60.0 if next_buses[0].delay is not None else None
            attrs[ATTR_TRACKING_SOURCE] = next_buses[0].tracking_source
            if next_buses[0].position:
                attrs[ATTR_LATITUDE] = next_buses[0].position.latitude
                attrs[ATTR_LONGITUDE] = next_buses[0].position.longitude
        if len(next_buses) > 1:
            attrs[ATTR_NEXT_UP] = local_time_string(next_buses[1].arrival_time)
            attrs[ATTR_NEXT_UP_DUE_IN] = due_in_minutes(next_buses[1].arrival_time)
            attrs[ATTR_NEXT_OCCUPANCY] = next_buses[1].occupancy
            attrs[ATTR_NEXT_DELAYED_BY] = next_buses[1].delay / 60.0 if next_buses[1].delay is not None else None
            attrs[ATTR_NEXT_TRACKING_SOURCE] = next_buses[1].tracking_source
        if next_buses:
            attrs[ATTR_UPCOMING_DEPARTURES] = [
                departure_attributes(detail) for detail in next_buses[:MAX_UPCOMING_DEPARTURES]
            ]
        return attrs

    def update(self):
        self.data.update()


class PublicTransportData:
    """Handle realtime and optional static GTFS data retrieval."""

    def __init__(
        self,
        trip_update_url,
        vehicle_position_url=None,
        headers=None,
        monitored_departures=None,
        static_schedule_url=None,
        stop_arrivals_url_template=None,
        transit_api_key=None,
        transit_departures=None,
    ):
        self._trip_update_url = trip_update_url
        self._vehicle_position_url = vehicle_position_url
        self._headers = headers or {}
        self._monitored_departures = monitored_departures or []
        self._stop_arrivals_url_template = stop_arrivals_url_template
        self._transit_api_key = transit_api_key
        self._transit_departures = transit_departures or []
        self._schedule_validator = (
            StaticScheduleValidator(static_schedule_url, self._monitored_departures, self._headers)
            if static_schedule_url
            else None
        )
        self.info = {}
        self.last_trip_update_error = None
        self.vehicle_feed_error = None
        self._schedule_status = {}
        self._last_stop_arrival_info = {}
        self._stop_arrivals_routes_by_stop = {}
        for route_id, stop_id in self._monitored_departures:
            routes = self._stop_arrivals_routes_by_stop.setdefault(stop_id, [])
            if route_id not in routes:
                routes.append(route_id)
        self._stop_arrivals_stop_ids = list(self._stop_arrivals_routes_by_stop)
        self._stop_arrivals_last_refresh = {}
        self._stop_arrivals_refresh_cursor = 0
        self._stop_arrivals_backoff_until = None
        self._last_transit_app_info = {}
        self._transit_app_backoff_until = None
        self._transit_app_last_refresh = None

    def get_schedule_status(self, route_id, stop_id):
        return self._schedule_status.get((route_id, stop_id))

    def get_route_label(self, route_id):
        if not self._schedule_validator:
            return None
        return self._schedule_validator.get_route_label(route_id)

    def get_departure_error(self, route_id, stop_id):
        error = getattr(self, "_primary_trip_update_error", self.last_trip_update_error)
        if not error:
            return None
        now = dt_util.now().astimezone(datetime.timezone.utc)
        # Coverage follows the configured bucket. A fallback for just one route
        # is not evidence that an all-routes departure has a healthy source.
        routes = [self.info.get(route_id, {})]
        if any(detail.arrival_time > now for stops in routes for detail in stops.get(stop_id, [])):
            return None
        refreshed = self._stop_arrivals_last_refresh.get(stop_id)
        if (self._stop_arrivals_url_template and refreshed
                and now - refreshed < MIN_TIME_BETWEEN_UPDATES * max(1, len(self._stop_arrivals_stop_ids))):
            return None
        mapped = any(route == route_id and stop == stop_id for route, stop, *_ in self._transit_departures)
        if (mapped and self._transit_app_last_refresh
                and now - self._transit_app_last_refresh < MIN_TRANSIT_APP_REFRESH_INTERVAL
                and not getattr(self, "transit_feed_error", None)):
            return None
        return error

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        self.last_trip_update_error = None
        self.info = {}

        if self._stop_arrivals_url_template:
            stop_arrivals_error = self._update_stop_arrival_statuses()
            if stop_arrivals_error:
                _LOGGER.warning("Falling back to trip updates after stop-level arrivals failure")
                self.last_trip_update_error = None
                stop_cache = self.info
                positions, vehicles_trips, occupancy = (
                    self._get_vehicle_positions() if self._vehicle_position_url else ({}, {}, {})
                )
                self._update_route_statuses(positions, vehicles_trips, occupancy)
                if self._has_departures(stop_cache):
                    self._merge_departure_times(stop_cache)
                if self.last_trip_update_error:
                    self.last_trip_update_error = f"{stop_arrivals_error}; {self.last_trip_update_error}"
            else:
                self.last_trip_update_error = getattr(self, "_stop_arrivals_error", None)
        else:
            positions, vehicles_trips, occupancy = (
                self._get_vehicle_positions() if self._vehicle_position_url else ({}, {}, {})
            )
            self._update_route_statuses(positions, vehicles_trips, occupancy)

        self._primary_trip_update_error = self.last_trip_update_error
        self.transit_feed_error = self._update_transit_app_statuses()
        # A healthy empty primary feed remains healthy when optional enrichment
        # fails. A partial fallback covers only its configured route/stop pairs.
        if self._monitored_departures:
            errors = [self.get_departure_error(route, stop) for route, stop in self._monitored_departures]
            self.last_trip_update_error = next((error for error in errors if error), None)
        elif self._has_departures(self.info):
            self.last_trip_update_error = None

        if self._schedule_validator:
            now = dt_util.now()
            self._schedule_status = {
                (route_id, stop_id): self._schedule_validator.get_status(route_id, stop_id, now)
                for route_id, stop_id in self._monitored_departures
            }
        else:
            self._schedule_status = {}

    def _update_stop_arrival_statuses(self):
        self._stop_arrivals_error = None
        now = dt_util.now().astimezone(datetime.timezone.utc)
        cached_departures = self._future_departure_times(
            self._last_stop_arrival_info,
            now,
            self._monitored_departures,
        )

        if self._stop_arrivals_backoff_until and now < self._stop_arrivals_backoff_until:
            self.info = cached_departures
            self._stop_arrivals_error = (
                "Stop-level arrivals temporarily rate limited until "
                f"{local_time_string(self._stop_arrivals_backoff_until)}"
            )
            return None if self._has_departures(cached_departures) else self._stop_arrivals_error

        departure_times = cached_departures
        stop_ids_to_refresh = self._get_stop_ids_to_refresh(now)

        if not stop_ids_to_refresh:
            self.info = departure_times
            self._last_stop_arrival_info = departure_times
            return None

        for stop_id in stop_ids_to_refresh:
            try:
                url = self._stop_arrivals_url_template.format(stop_id=stop_id)
                response = requests.get(url, headers=self._headers, timeout=REQUEST_TIMEOUT)
                if response.status_code == 429:
                    retry_after = self._get_stop_arrivals_retry_after(response)
                    self._stop_arrivals_backoff_until = now + retry_after
                    self.info = departure_times
                    self._last_stop_arrival_info = departure_times
                    _LOGGER.warning(
                        "Stop-level arrivals rate limited; backing off until %s",
                        local_time_string(self._stop_arrivals_backoff_until),
                    )
                    self._stop_arrivals_error = (
                        "Stop-level arrivals temporarily rate limited until "
                        f"{local_time_string(self._stop_arrivals_backoff_until)}"
                    )
                    return None if self._has_departures(departure_times) else self._stop_arrivals_error
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Invalid stop arrivals payload")
                if payload.get("code") not in (None, 200):
                    code = payload.get("code")
                    safe_code = code if isinstance(code, int) else "invalid"
                    self.info = departure_times
                    self._last_stop_arrival_info = departure_times
                    self.last_trip_update_error = f"Stop-level arrivals unavailable: API code {safe_code}"
                    _LOGGER.error("Unexpected stop-level arrivals payload code: %s", safe_code)
                    return self.last_trip_update_error
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ValueError("Invalid stop arrivals data")
                entry = data.get("entry")
                if not isinstance(entry, dict):
                    raise ValueError("Invalid stop arrivals entry")
                arrivals = entry.get("arrivalsAndDepartures")
                if not isinstance(arrivals, list):
                    raise ValueError("Invalid stop arrivals list")
            except Exception as err:
                self.info = departure_times
                self._last_stop_arrival_info = departure_times
                self.last_trip_update_error = f"Stop-level arrivals unavailable ({type(err).__name__})"
                _LOGGER.error("Unable to refresh stop-level arrivals (%s)", type(err).__name__)
                return self.last_trip_update_error

            for route_id in self._stop_arrivals_routes_by_stop.get(stop_id, []):
                departure_times.setdefault(route_id, {})
                departure_times[route_id][stop_id] = filter_onebusaway_arrivals(arrivals, route_id, now)
            self._stop_arrivals_last_refresh[stop_id] = now

        self.info = departure_times
        self._last_stop_arrival_info = departure_times
        self._stop_arrivals_backoff_until = None
        return None

    def _update_transit_app_statuses(self):
        if not self._transit_api_key or not self._transit_departures:
            return None

        now = dt_util.now().astimezone(datetime.timezone.utc)
        cached_departures = self._future_departure_times(
            self._last_transit_app_info,
            now,
            None,
        )

        if self._transit_app_backoff_until and now < self._transit_app_backoff_until:
            self._merge_departure_times(cached_departures)
            if self._has_departures(cached_departures):
                return None
            return (
                "Transit app departures temporarily rate limited until "
                f"{local_time_string(self._transit_app_backoff_until)}"
            )

        if (
            self._transit_app_last_refresh
            and now - self._transit_app_last_refresh < MIN_TRANSIT_APP_REFRESH_INTERVAL
        ):
            self._merge_departure_times(cached_departures)
            return None

        global_stop_ids = sorted(
            {
                global_stop_id
                for _route, _stop, global_stop_id, _transit_route in self._transit_departures
            }
        )
        route_departures = []
        try:
            for stop_id_batch in self._chunked(
                global_stop_ids,
                TRANSIT_API_MAX_STOP_IDS_PER_REQUEST,
            ):
                response = requests.get(
                    TRANSIT_API_STOP_DEPARTURES_URL,
                    params={
                        "global_stop_ids": ",".join(stop_id_batch),
                        "max_num_departures": str(
                            min(MAX_UPCOMING_DEPARTURES, TRANSIT_API_MAX_DEPARTURES_PER_REQUEST)
                        ),
                        "should_update_realtime": "true",
                        "remove_cancelled": "false",
                    },
                    headers={
                        "apiKey": self._transit_api_key,
                        "Accept": "application/json",
                        "Accept-Language": "en",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 429:
                    retry_after = self._get_stop_arrivals_retry_after(response)
                    self._transit_app_backoff_until = now + retry_after
                    self._merge_departure_times(cached_departures)
                    self._last_transit_app_info = cached_departures
                    _LOGGER.warning(
                        "Transit app departures rate limited; backing off until %s",
                        local_time_string(self._transit_app_backoff_until),
                    )
                    if self._has_departures(cached_departures):
                        return None
                    return (
                        "Transit app departures temporarily rate limited until "
                        f"{local_time_string(self._transit_app_backoff_until)}"
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("route_departures"), list):
                    raise ValueError("Invalid Transit departures payload")
                route_departures.extend(payload["route_departures"])
        except Exception as err:
            _LOGGER.warning("Unable to refresh Transit app departures (%s)", type(err).__name__)
            self._merge_departure_times(cached_departures)
            self._last_transit_app_info = cached_departures
            return f"Transit app departures unavailable ({type(err).__name__})"

        transit_departure_times = {}
        for route_id, stop_id, global_stop_id, transit_route in self._transit_departures:
            transit_departure_times.setdefault(route_id, {})
            transit_departure_times[route_id][stop_id] = filter_transit_app_departures(
                route_departures,
                global_stop_id=global_stop_id,
                configured_route=transit_route,
                now=now,
            )

        self._merge_departure_times(transit_departure_times)
        self._last_transit_app_info = transit_departure_times
        self._transit_app_backoff_until = None
        self._transit_app_last_refresh = now
        return None

    def _get_stop_ids_to_refresh(self, now):
        if not self._stop_arrivals_stop_ids:
            return []

        refresh_interval = MIN_TIME_BETWEEN_UPDATES * len(self._stop_arrivals_stop_ids)
        stale_stop_ids = [
            stop_id
            for stop_id in self._stop_arrivals_stop_ids
            if stop_id not in self._stop_arrivals_last_refresh
            or now - self._stop_arrivals_last_refresh[stop_id] >= refresh_interval
        ]

        if not stale_stop_ids:
            return []

        if len(self._stop_arrivals_last_refresh) < len(self._stop_arrivals_stop_ids):
            return stale_stop_ids

        for offset in range(len(self._stop_arrivals_stop_ids)):
            index = (self._stop_arrivals_refresh_cursor + offset) % len(self._stop_arrivals_stop_ids)
            stop_id = self._stop_arrivals_stop_ids[index]
            if stop_id in stale_stop_ids:
                self._stop_arrivals_refresh_cursor = (index + 1) % len(self._stop_arrivals_stop_ids)
                return [stop_id]

        return []

    @staticmethod
    def _future_departure_times(departure_times, now, monitored_departures=None):
        future_departures = {}
        for route_id, stop_id in monitored_departures or []:
            future_departures.setdefault(route_id, {})
            future_departures[route_id].setdefault(stop_id, [])
        for route_id, stops in (departure_times or {}).items():
            future_departures.setdefault(route_id, {})
            for stop_id, details in stops.items():
                filtered_details = [
                    detail for detail in details if detail.arrival_time > now
                ]
                combined = combine_duplicate_departures(filtered_details, match_routes=not route_id)
                future_departures[route_id][stop_id] = [detail for detail in combined if detail.arrival_time > now]
        return future_departures

    @staticmethod
    def _has_departures(departure_times):
        return any(
            details
            for stops in (departure_times or {}).values()
            for details in stops.values()
        )

    def _merge_departure_times(self, departure_times):
        for route_id, stops in (departure_times or {}).items():
            self.info.setdefault(route_id, {})
            for stop_id, details in stops.items():
                existing = self.info[route_id].get(stop_id, [])
                self.info[route_id][stop_id] = combine_duplicate_departures(
                    list(existing) + list(details or []), match_routes=not route_id
                )

    @staticmethod
    def _get_stop_arrivals_retry_after(response):
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                seconds = int(retry_after)
            except ValueError:
                seconds = None
            if seconds is not None and seconds > 0:
                return datetime.timedelta(seconds=seconds)
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                seconds = (
                    retry_at - datetime.datetime.now(datetime.timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError):
                seconds = None
            if seconds is not None and seconds > 0:
                return datetime.timedelta(seconds=seconds)
        return DEFAULT_STOP_ARRIVALS_BACKOFF

    @staticmethod
    def _chunked(items, size):
        for index in range(0, len(items), size):
            yield items[index : index + size]

    def _update_route_statuses(self, vehicle_positions, vehicles_trips, vehicle_occupancy):
        from google.transit import gtfs_realtime_pb2

        try:
            feed = fetch_feed(self._trip_update_url, self._headers)
        except FeedFetchError as err:
            self.last_trip_update_error = f"Realtime trip updates unavailable ({err})"
            _LOGGER.error("Unable to refresh realtime trip updates (%s)", err)
            return

        departure_times = {}
        now = dt_util.now().timestamp()

        for entity in feed.entity:
            if entity.is_deleted or not entity.HasField("trip_update"):
                continue
            trip_update = entity.trip_update
            if gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(
                trip_update.trip.schedule_relationship
            ) in {"CANCELED", "DELETED"}:
                continue

            trip = trip_update.trip
            trip_vehicle = vehicles_trips.for_trip(trip) if isinstance(vehicles_trips, VehicleIndex) else None
            route_id = trip.route_id or (trip_vehicle.route_id if trip_vehicle else "")
            vehicle_id = trip_update.vehicle.id or (
                trip_vehicle.vehicle_id if trip_vehicle else
                None if isinstance(vehicles_trips, VehicleIndex) else vehicles_trips.get(trip.trip_id)
            )
            position = vehicle_positions.get(vehicle_id)
            occupancy = vehicle_occupancy.get(vehicle_id)
            if trip_vehicle and not trip_update.vehicle.id:
                position, occupancy = trip_vehicle.position, trip_vehicle.occupancy
            for stop in trip_update.stop_time_update:
                if not stop.stop_id or stop.schedule_relationship in (
                    gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED,
                    gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.NO_DATA,
                ):
                    continue
                # Departure is authoritative when supplied; a past departure
                # must not be resurrected by a contradictory future arrival.
                event = stop.departure if stop.departure.HasField("time") else stop.arrival
                if not event.HasField("time") or event.time <= now:
                    continue
                try:
                    arrival_time = datetime.datetime.fromtimestamp(event.time, datetime.timezone.utc)
                except (ValueError, OverflowError, OSError):
                    continue
                details = StopDetails(
                    arrival_time,
                    position,
                    occupancy,
                    event.delay if event.HasField("delay") else None,
                    TRACKING_SOURCE_GTFS_RT,
                    True,
                    trip.trip_id or None,
                    trip_start_date=trip.start_date or None,
                    trip_start_time=trip.start_time or None,
                    route_id=route_id or None,
                )
                departure_times.setdefault(route_id, {}).setdefault(stop.stop_id, []).append(details)

        for route in departure_times:
            for stop in departure_times[route]:
                departure_times[route][stop] = combine_duplicate_departures(
                    departure_times[route][stop], match_routes=not route
                )

        self.info = departure_times

    def _get_vehicle_positions(self):
        try:
            feed = fetch_feed(self._vehicle_position_url, self._headers)
        except FeedFetchError as err:
            if self.vehicle_feed_error is None:
                _LOGGER.warning("Unable to refresh vehicle positions (%s)", err)
            self.vehicle_feed_error = str(err)
            return {}, {}, {}
        if self.vehicle_feed_error is not None:
            _LOGGER.info("Vehicle positions feed is available again")
        self.vehicle_feed_error = None

        positions = {}
        vehicles_trips = VehicleIndex(feed)
        occupancy = {}

        for entity in feed.entity:
            if entity.is_deleted or not entity.HasField("vehicle"):
                continue
            vehicle = entity.vehicle
            vehicle_id = vehicle.vehicle.id
            if not vehicle_id:
                continue
            position = valid_position(
                vehicle.position.latitude if vehicle.position.HasField("latitude") else None,
                vehicle.position.longitude if vehicle.position.HasField("longitude") else None,
            )
            if position is not None:
                positions[vehicle_id] = position
            if vehicle.HasField("occupancy_status"):
                try:
                    occupancy[vehicle_id] = OccupancyStatus(vehicle.occupancy_status).name
                except ValueError:
                    pass

        return positions, vehicles_trips, occupancy
