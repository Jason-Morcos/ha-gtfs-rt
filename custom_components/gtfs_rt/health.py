from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import zipfile
from collections import defaultdict
from dataclasses import dataclass

import requests

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCHEDULE_REFRESH = dt.timedelta(hours=12)
DEFAULT_ACTIVE_WINDOW_BEFORE = dt.timedelta(minutes=30)
DEFAULT_ACTIVE_WINDOW_AFTER = dt.timedelta(minutes=90)

STATUS_LOOKUP_FAILED = "schedule_lookup_failed"
STATUS_INVALID_ROUTE = "invalid_route"
STATUS_INVALID_STOP = "invalid_stop"
STATUS_ROUTE_STOP_MISMATCH = "route_stop_mismatch"
STATUS_NO_SERVICE_TODAY = "no_service_today"
STATUS_NO_SERVICE_NOW = "no_service_now"
STATUS_SERVICE_EXPECTED = "service_expected"


def parse_gtfs_seconds(value: str) -> int:
    """Parse a GTFS HH:MM:SS string into total seconds."""
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True)
class ScheduleStatus:
    status: str
    route_exists: bool
    stop_exists: bool
    route_serves_stop: bool
    service_today: bool
    service_expected_now: bool
    next_scheduled_departure: dt.datetime | None
    problem_reason: str | None = None

    @property
    def is_config_problem(self) -> bool:
        return self.status in {
            STATUS_INVALID_ROUTE,
            STATUS_INVALID_STOP,
            STATUS_ROUTE_STOP_MISMATCH,
        }


class StaticScheduleValidator:
    """Validate monitored route/stop pairs against a static GTFS feed."""

    def __init__(
        self,
        schedule_url: str,
        monitored_departures: list[tuple[str, str]],
        headers: dict[str, str] | None = None,
        refresh_interval: dt.timedelta = DEFAULT_SCHEDULE_REFRESH,
        active_window_before: dt.timedelta = DEFAULT_ACTIVE_WINDOW_BEFORE,
        active_window_after: dt.timedelta = DEFAULT_ACTIVE_WINDOW_AFTER,
    ) -> None:
        self._schedule_url = schedule_url
        self._headers = headers or {}
        self._refresh_interval = refresh_interval
        self._active_window_before = active_window_before
        self._active_window_after = active_window_after
        self._monitored_routes = {route for route, _ in monitored_departures}
        self._monitored_stops = {stop for _, stop in monitored_departures}

        self._last_refresh: dt.datetime | None = None
        self._route_ids: set[str] = set()
        self._stop_ids: set[str] = set()
        self._route_stop_service_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._departures_by_service: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        self._calendar: dict[str, tuple[set[int], dt.date, dt.date]] = {}
        self._calendar_exceptions: dict[dt.date, dict[str, bool]] = defaultdict(dict)

    def get_status(self, route_id: str, stop_id: str, now: dt.datetime) -> ScheduleStatus:
        """Return the schedule-aware health for a route/stop pair."""
        try:
            self._ensure_loaded(now)
        except Exception as err:  # pragma: no cover - defensive fallback
            _LOGGER.warning("Unable to validate static GTFS schedule: %s", err)
            return ScheduleStatus(
                status=STATUS_LOOKUP_FAILED,
                route_exists=False,
                stop_exists=False,
                route_serves_stop=False,
                service_today=False,
                service_expected_now=False,
                next_scheduled_departure=None,
                problem_reason=str(err),
            )

        route_exists = route_id in self._route_ids
        stop_exists = stop_id in self._stop_ids
        route_serves_stop = bool(self._route_stop_service_ids.get((route_id, stop_id)))

        if not route_exists:
            return ScheduleStatus(
                status=STATUS_INVALID_ROUTE,
                route_exists=False,
                stop_exists=stop_exists,
                route_serves_stop=False,
                service_today=False,
                service_expected_now=False,
                next_scheduled_departure=None,
                problem_reason=f"Route {route_id} is not present in the static GTFS feed",
            )

        if not stop_exists:
            return ScheduleStatus(
                status=STATUS_INVALID_STOP,
                route_exists=True,
                stop_exists=False,
                route_serves_stop=False,
                service_today=False,
                service_expected_now=False,
                next_scheduled_departure=None,
                problem_reason=f"Stop {stop_id} is not present in the static GTFS feed",
            )

        if not route_serves_stop:
            return ScheduleStatus(
                status=STATUS_ROUTE_STOP_MISMATCH,
                route_exists=True,
                stop_exists=True,
                route_serves_stop=False,
                service_today=False,
                service_expected_now=False,
                next_scheduled_departure=None,
                problem_reason=f"Route {route_id} does not serve stop {stop_id} in the static GTFS feed",
            )

        service_ids = self._active_service_ids(now.date())
        departures = [
            departure_seconds
            for service_id in service_ids
            for departure_seconds in self._departures_by_service.get((route_id, stop_id, service_id), [])
        ]
        departures.sort()

        if not departures:
            return ScheduleStatus(
                status=STATUS_NO_SERVICE_TODAY,
                route_exists=True,
                stop_exists=True,
                route_serves_stop=True,
                service_today=False,
                service_expected_now=False,
                next_scheduled_departure=None,
            )

        now_seconds = now.hour * 3600 + now.minute * 60 + now.second
        next_departure_seconds = next((departure for departure in departures if departure >= now_seconds), None)
        next_departure = (
            dt.datetime.combine(now.date(), dt.time.min, tzinfo=now.tzinfo)
            + dt.timedelta(seconds=next_departure_seconds)
            if next_departure_seconds is not None
            else None
        )

        active_lower_bound = now_seconds - int(self._active_window_before.total_seconds())
        active_upper_bound = now_seconds + int(self._active_window_after.total_seconds())
        service_expected_now = any(
            active_lower_bound <= departure <= active_upper_bound for departure in departures
        )

        return ScheduleStatus(
            status=STATUS_SERVICE_EXPECTED if service_expected_now else STATUS_NO_SERVICE_NOW,
            route_exists=True,
            stop_exists=True,
            route_serves_stop=True,
            service_today=True,
            service_expected_now=service_expected_now,
            next_scheduled_departure=next_departure,
        )

    def _ensure_loaded(self, now: dt.datetime) -> None:
        if self._last_refresh and now - self._last_refresh < self._refresh_interval:
            return

        response = requests.get(self._schedule_url, headers=self._headers, timeout=30)
        response.raise_for_status()
        self._load_schedule_from_bytes(response.content)
        self._last_refresh = now

    def _load_schedule_from_bytes(self, archive_bytes: bytes) -> None:
        self._route_ids.clear()
        self._stop_ids.clear()
        self._route_stop_service_ids.clear()
        self._departures_by_service.clear()
        self._calendar.clear()
        self._calendar_exceptions.clear()

        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))

        trip_to_route_service: dict[str, tuple[str, str]] = {}

        with archive.open("routes.txt") as route_file:
            for row in csv.DictReader(io.TextIOWrapper(route_file, "utf-8-sig")):
                route_id = row["route_id"]
                if route_id in self._monitored_routes:
                    self._route_ids.add(route_id)

        with archive.open("stops.txt") as stop_file:
            for row in csv.DictReader(io.TextIOWrapper(stop_file, "utf-8-sig")):
                stop_id = row["stop_id"]
                if stop_id in self._monitored_stops:
                    self._stop_ids.add(stop_id)

        with archive.open("trips.txt") as trip_file:
            for row in csv.DictReader(io.TextIOWrapper(trip_file, "utf-8-sig")):
                route_id = row["route_id"]
                if route_id not in self._monitored_routes:
                    continue
                trip_to_route_service[row["trip_id"]] = (route_id, row["service_id"])

        with archive.open("stop_times.txt") as stop_time_file:
            for row in csv.DictReader(io.TextIOWrapper(stop_time_file, "utf-8-sig")):
                trip_id = row["trip_id"]
                trip_details = trip_to_route_service.get(trip_id)
                if trip_details is None:
                    continue

                stop_id = row["stop_id"]
                if stop_id not in self._monitored_stops:
                    continue

                route_id, service_id = trip_details
                departure_value = row["departure_time"] or row["arrival_time"]
                if not departure_value:
                    continue

                departure_seconds = parse_gtfs_seconds(departure_value)
                self._route_stop_service_ids[(route_id, stop_id)].add(service_id)
                self._departures_by_service[(route_id, stop_id, service_id)].append(departure_seconds)

        for departure_list in self._departures_by_service.values():
            departure_list.sort()

        with archive.open("calendar.txt") as calendar_file:
            for row in csv.DictReader(io.TextIOWrapper(calendar_file, "utf-8-sig")):
                weekdays = {
                    index
                    for index, column in enumerate(
                        [
                            "monday",
                            "tuesday",
                            "wednesday",
                            "thursday",
                            "friday",
                            "saturday",
                            "sunday",
                        ]
                    )
                    if row[column] == "1"
                }
                self._calendar[row["service_id"]] = (
                    weekdays,
                    dt.datetime.strptime(row["start_date"], "%Y%m%d").date(),
                    dt.datetime.strptime(row["end_date"], "%Y%m%d").date(),
                )

        with archive.open("calendar_dates.txt") as calendar_dates_file:
            for row in csv.DictReader(io.TextIOWrapper(calendar_dates_file, "utf-8-sig")):
                service_date = dt.datetime.strptime(row["date"], "%Y%m%d").date()
                self._calendar_exceptions[service_date][row["service_id"]] = row["exception_type"] == "1"

    def _active_service_ids(self, service_date: dt.date) -> set[str]:
        active_ids = {
            service_id
            for service_id, (weekdays, start_date, end_date) in self._calendar.items()
            if start_date <= service_date <= end_date and service_date.weekday() in weekdays
        }

        for service_id, enabled in self._calendar_exceptions.get(service_date, {}).items():
            if enabled:
                active_ids.add(service_id)
            else:
                active_ids.discard(service_id)

        return active_ids
