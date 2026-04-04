from __future__ import annotations

import datetime
import logging
import time
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
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    DEFAULT_NAME,
    DOMAIN,
    ICON,
    REQUEST_TIMEOUT,
    TIME_STR_FORMAT,
)
from .health import STATUS_LOOKUP_FAILED, STATUS_SERVICE_EXPECTED, StaticScheduleValidator
from .realtime import StopDetails, filter_onebusaway_arrivals

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
ATTR_UPCOMING_DEPARTURES = "Upcoming departures"
ATTR_SERVICE_STATUS = "Service status"
ATTR_SERVICE_TODAY = "Service today"
ATTR_SERVICE_EXPECTED_NOW = "Service expected now"
ATTR_NEXT_SCHEDULED_DEPARTURE = "Next scheduled departure"
ATTR_PROBLEM_REASON = "Problem reason"

MIN_TIME_BETWEEN_UPDATES = datetime.timedelta(seconds=60)
DEFAULT_STOP_ARRIVALS_BACKOFF = datetime.timedelta(minutes=5)
MAX_UPCOMING_DEPARTURES = 5


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
    diff = timestamp - dt_util.now().replace(tzinfo=None)
    return int(diff.total_seconds() / 60)


def departure_attributes(detail):
    """Serialize a realtime departure into state attributes."""
    attrs = {
        "due_at": detail.arrival_time.strftime(TIME_STR_FORMAT),
        "due_in": due_in_minutes(detail.arrival_time),
        "delay_minutes": detail.delay / 60.0 if detail.delay else None,
        "occupancy": detail.occupancy,
    }
    if detail.position:
        attrs["latitude"] = detail.position.latitude
        attrs["longitude"] = detail.position.longitude
    return attrs


def _build_shared_data(config):
    monitored_departures = [
        (departure[CONF_ROUTE], departure[CONF_STOP_ID])
        for departure in config[CONF_DEPARTURES]
    ]
    return PublicTransportData(
        config[CONF_TRIP_UPDATE_URL],
        config.get(CONF_VEHICLE_POSITION_URL),
        config.get(CONF_HEADERS, {}),
        monitored_departures,
        config.get(CONF_STATIC_SCHEDULE_URL),
        config.get(CONF_STOP_ARRIVALS_URL_TEMPLATE),
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
    add_devices(_build_sensors(data, normalized), True)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up GTFS-Realtime sensors from a config entry."""
    config = dict(config_entry.data)
    data = _build_shared_data(config)
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = data
    async_add_entities(_build_sensors(data, config, config_entry), True)


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

    def _get_next_buses(self):
        return self.data.info.get(self._route, {}).get(self._stop, [])

    def _get_schedule_status(self):
        return self.data.get_schedule_status(self._route, self._stop)

    def _get_problem_reason(self, schedule_status, next_buses):
        if self.data.last_trip_update_error:
            return self.data.last_trip_update_error
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
            last_trip_update_error=self.data.last_trip_update_error,
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
            ATTR_UPCOMING_DEPARTURES: [],
            ATTR_STOP_ID: self._stop,
            ATTR_ROUTE: self._route,
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
            attrs[ATTR_DUE_AT] = next_buses[0].arrival_time.strftime(TIME_STR_FORMAT)
            attrs[ATTR_OCCUPANCY] = next_buses[0].occupancy
            attrs[ATTR_DELAYED_BY] = next_buses[0].delay / 60.0 if next_buses[0].delay else None
            if next_buses[0].position:
                attrs[ATTR_LATITUDE] = next_buses[0].position.latitude
                attrs[ATTR_LONGITUDE] = next_buses[0].position.longitude
        if len(next_buses) > 1:
            attrs[ATTR_NEXT_UP] = next_buses[1].arrival_time.strftime(TIME_STR_FORMAT)
            attrs[ATTR_NEXT_UP_DUE_IN] = due_in_minutes(next_buses[1].arrival_time)
            attrs[ATTR_NEXT_OCCUPANCY] = next_buses[1].occupancy
            attrs[ATTR_NEXT_DELAYED_BY] = next_buses[1].delay / 60.0 if next_buses[1].delay else None
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
    ):
        self._trip_update_url = trip_update_url
        self._vehicle_position_url = vehicle_position_url
        self._headers = headers or {}
        self._monitored_departures = monitored_departures or []
        self._stop_arrivals_url_template = stop_arrivals_url_template
        self._schedule_validator = (
            StaticScheduleValidator(static_schedule_url, self._monitored_departures, self._headers)
            if static_schedule_url
            else None
        )
        self.info = {}
        self.last_trip_update_error = None
        self._schedule_status = {}
        self._last_stop_arrival_info = {}
        self._stop_arrivals_backoff_until = None

    def get_schedule_status(self, route_id, stop_id):
        return self._schedule_status.get((route_id, stop_id))

    def get_route_label(self, route_id):
        if not self._schedule_validator:
            return None
        return self._schedule_validator.get_route_label(route_id)

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        self.last_trip_update_error = None
        self.info = {}

        if self._stop_arrivals_url_template:
            stop_arrivals_error = self._update_stop_arrival_statuses()
            if stop_arrivals_error:
                _LOGGER.warning("Falling back to trip updates after stop-level arrivals failure")
                self.last_trip_update_error = None
                positions, vehicles_trips, occupancy = (
                    self._get_vehicle_positions() if self._vehicle_position_url else ({}, {}, {})
                )
                self._update_route_statuses(positions, vehicles_trips, occupancy)
                if self.last_trip_update_error:
                    self.last_trip_update_error = f"{stop_arrivals_error}; {self.last_trip_update_error}"
        else:
            positions, vehicles_trips, occupancy = (
                self._get_vehicle_positions() if self._vehicle_position_url else ({}, {}, {})
            )
            self._update_route_statuses(positions, vehicles_trips, occupancy)

        if self._schedule_validator:
            now = dt_util.now()
            self._schedule_status = {
                (route_id, stop_id): self._schedule_validator.get_status(route_id, stop_id, now)
                for route_id, stop_id in self._monitored_departures
            }
        else:
            self._schedule_status = {}

    def _update_stop_arrival_statuses(self):
        now = dt_util.now().replace(tzinfo=None)
        cached_departures = self._future_departure_times(self._last_stop_arrival_info, now)

        if self._stop_arrivals_backoff_until and now < self._stop_arrivals_backoff_until:
            self.info = cached_departures
            if self._has_departures(cached_departures):
                return None
            return (
                "Stop-level arrivals temporarily rate limited until "
                f"{self._stop_arrivals_backoff_until.strftime(TIME_STR_FORMAT)}"
            )

        departure_times = {}

        for route_id, stop_id in self._monitored_departures:
            departure_times.setdefault(route_id, {})
            departure_times[route_id][stop_id] = []
            try:
                url = self._stop_arrivals_url_template.format(stop_id=stop_id)
                response = requests.get(url, headers=self._headers, timeout=REQUEST_TIMEOUT)
                if response.status_code == 429:
                    retry_after = self._get_stop_arrivals_retry_after(response)
                    self._stop_arrivals_backoff_until = now + retry_after
                    self.info = cached_departures
                    _LOGGER.warning(
                        "Stop-level arrivals rate limited; backing off until %s",
                        self._stop_arrivals_backoff_until.strftime(TIME_STR_FORMAT),
                    )
                    if self._has_departures(cached_departures):
                        return None
                    return (
                        "Stop-level arrivals temporarily rate limited until "
                        f"{self._stop_arrivals_backoff_until.strftime(TIME_STR_FORMAT)}"
                    )
                response.raise_for_status()
                payload = response.json()
            except Exception as err:
                self.last_trip_update_error = f"Stop-level arrivals unavailable: {err}"
                _LOGGER.error("Unable to refresh stop-level arrivals: %s", err)
                return self.last_trip_update_error

            if payload.get("code") not in (None, 200):
                self.last_trip_update_error = f"Stop-level arrivals unavailable: API code {payload.get('code')}"
                _LOGGER.error("Unexpected stop-level arrivals payload code: %s", payload.get("code"))
                return self.last_trip_update_error

            entry = (payload.get("data") or {}).get("entry") or {}
            arrivals = entry.get("arrivalsAndDepartures") or []
            departure_times[route_id][stop_id] = filter_onebusaway_arrivals(arrivals, route_id, now)

        self.info = departure_times
        self._last_stop_arrival_info = departure_times
        self._stop_arrivals_backoff_until = None
        return None

    @staticmethod
    def _future_departure_times(departure_times, now):
        future_departures = {}
        for route_id, stops in (departure_times or {}).items():
            future_departures[route_id] = {}
            for stop_id, details in stops.items():
                future_departures[route_id][stop_id] = [
                    detail for detail in details if detail.arrival_time > now
                ]
        return future_departures

    @staticmethod
    def _has_departures(departure_times):
        return any(
            details
            for stops in (departure_times or {}).values()
            for details in stops.values()
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
        return DEFAULT_STOP_ARRIVALS_BACKOFF

    def _update_route_statuses(self, vehicle_positions, vehicles_trips, vehicle_occupancy):
        from google.transit import gtfs_realtime_pb2

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            response = requests.get(self._trip_update_url, headers=self._headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            feed.ParseFromString(response.content)
        except Exception as err:
            self.last_trip_update_error = f"Realtime trip updates unavailable: {err}"
            _LOGGER.error("Unable to refresh realtime trip updates: %s", err)
            return

        departure_times = {}

        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue

            route_id = entity.trip_update.trip.route_id
            vehicle_id = entity.trip_update.vehicle.id
            if not vehicle_id:
                vehicle_id = vehicles_trips.get(entity.trip_update.trip.trip_id)

            if route_id not in departure_times:
                departure_times[route_id] = {}

            for stop in entity.trip_update.stop_time_update:
                stop_id = stop.stop_id
                if not departure_times[route_id].get(stop_id):
                    departure_times[route_id][stop_id] = []

                if int(stop.departure.time) > int(time.time()):
                    details = StopDetails(
                        datetime.datetime.fromtimestamp(stop.departure.time),
                        vehicle_positions.get(vehicle_id),
                        vehicle_occupancy.get(vehicle_id),
                        stop.departure.delay,
                    )
                    departure_times[route_id][stop_id].append(details)
                elif int(stop.arrival.time) > int(time.time()):
                    details = StopDetails(
                        datetime.datetime.fromtimestamp(stop.arrival.time),
                        vehicle_positions.get(vehicle_id),
                        vehicle_occupancy.get(vehicle_id),
                        stop.arrival.delay,
                    )
                    departure_times[route_id][stop_id].append(details)

        for route in departure_times:
            for stop in departure_times[route]:
                departure_times[route][stop].sort(key=lambda item: item.arrival_time)

        self.info = departure_times

    def _get_vehicle_positions(self):
        from google.transit import gtfs_realtime_pb2

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            response = requests.get(
                self._vehicle_position_url,
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            feed.ParseFromString(response.content)
        except Exception as err:
            _LOGGER.warning("Unable to refresh vehicle positions: %s", err)
            return {}, {}, {}

        positions = {}
        vehicles_trips = {}
        occupancy = {}

        for entity in feed.entity:
            vehicle = entity.vehicle
            if not vehicle.trip.route_id:
                continue
            positions[vehicle.vehicle.id] = vehicle.position
            vehicles_trips[vehicle.trip.trip_id] = vehicle.vehicle.id
            occupancy[vehicle.vehicle.id] = OccupancyStatus(vehicle.occupancy_status).name

        return positions, vehicles_trips, occupancy
