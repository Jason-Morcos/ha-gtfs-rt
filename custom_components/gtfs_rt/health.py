from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests

_LOGGER = logging.getLogger(__name__)

FAILED_SCHEDULE_RETRY = dt.timedelta(minutes=5)

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
        self._last_attempt: dt.datetime | None = None
        self._load_error: str | None = None
        self._agency_timezone = None
        self._route_ids: set[str] = set()
        self._stop_ids: set[str] = set()
        self._route_labels: dict[str, str] = {}
        self._route_stop_service_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._departures_by_service: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        self._calendar: dict[str, tuple[set[int], dt.date, dt.date]] = {}
        self._calendar_exceptions: dict[dt.date, dict[str, bool]] = defaultdict(dict)

    def get_status(self, route_id: str, stop_id: str, now: dt.datetime) -> ScheduleStatus:
        """Return the schedule-aware health for a route/stop pair."""
        try:
            self._ensure_loaded(now)
        except Exception as err:  # pragma: no cover - defensive fallback
            # The loader logs once per attempt, without URL or credential content.
            return ScheduleStatus(
                status=STATUS_LOOKUP_FAILED,
                route_exists=False,
                stop_exists=False,
                route_serves_stop=False,
                service_today=False,
                service_expected_now=False,
                next_scheduled_departure=None,
                problem_reason=f"Static schedule unavailable ({type(err).__name__})",
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

        local_now = now.astimezone(self._agency_timezone) if self._agency_timezone else now
        timezone = local_now.tzinfo
        today = local_now.date()
        now_instant = local_now.astimezone(dt.timezone.utc) if timezone else local_now
        lower = now_instant - self._active_window_before
        upper = now_instant + self._active_window_after
        midnight = dt.datetime.combine(today, dt.time.min, tzinfo=timezone)
        tomorrow = midnight + dt.timedelta(days=1)
        if timezone:
            midnight = midnight.astimezone(dt.timezone.utc)
            tomorrow = tomorrow.astimezone(dt.timezone.utc)
        max_seconds = max(
            (max(values) for (route, stop, _), values in self._departures_by_service.items()
             if route == route_id and stop == stop_id and values),
            default=0,
        )
        departures = []
        service_today = False
        # Include carry-over service days for GTFS times such as 25:10:00,
        # plus the following day when it falls within the active window.
        previous_days = max_seconds // 86400 + 1
        following_days = int(self._active_window_after.total_seconds() // 86400) + 1
        for offset in range(-previous_days, following_days + 1):
            service_date = today + dt.timedelta(days=offset)
            # GTFS times are elapsed seconds from local noon minus 12 hours.
            # Calculate in UTC so DST gaps/folds cannot alter elapsed time.
            anchor = dt.datetime.combine(service_date, dt.time(12), tzinfo=timezone)
            if timezone:
                anchor = anchor.astimezone(dt.timezone.utc)
            anchor -= dt.timedelta(hours=12)
            for service_id in self._active_service_ids(service_date):
                for seconds in self._departures_by_service.get((route_id, stop_id, service_id), []):
                    departure = anchor + dt.timedelta(seconds=seconds)
                    if offset == 0 or midnight <= departure < tomorrow:
                        service_today = True
                    current_service_day = offset == 0
                    carries_over = offset < 0 and departure >= midnight - self._active_window_before
                    within_active_window = lower <= departure <= upper
                    if current_service_day or carries_over or within_active_window:
                        departures.append(departure)

        departures.sort()
        service_expected_now = any(lower <= departure <= upper for departure in departures)
        next_departure = next((departure for departure in departures if departure >= now_instant), None)
        if next_departure is not None and timezone:
            # Entity attributes are displayed in the caller's HA timezone.
            next_departure = next_departure.astimezone(now.tzinfo)
        status = (
            STATUS_SERVICE_EXPECTED if service_expected_now
            else STATUS_NO_SERVICE_NOW if service_today
            else STATUS_NO_SERVICE_TODAY
        )
        return ScheduleStatus(
            status=status,
            route_exists=True,
            stop_exists=True,
            route_serves_stop=True,
            service_today=service_today,
            service_expected_now=service_expected_now,
            next_scheduled_departure=next_departure,
        )

    def get_route_label(self, route_id: str) -> str | None:
        """Return a human-friendly route label when the static feed provides one."""
        return self._route_labels.get(route_id)

    def _ensure_loaded(self, now: dt.datetime) -> None:
        if self._last_refresh and now - self._last_refresh < self._refresh_interval:
            return

        if self._load_error and self._last_attempt and now - self._last_attempt < FAILED_SCHEDULE_RETRY:
            raise RuntimeError("Static schedule retry pending")
        self._last_attempt = now
        try:
            response = requests.get(self._schedule_url, headers=self._headers, timeout=30)
            response.raise_for_status()
            self._load_schedule_from_bytes(response.content)
        except Exception as err:
            self._load_error = type(err).__name__
            _LOGGER.warning("Unable to validate static GTFS schedule (%s)", self._load_error)
            raise
        self._last_refresh = now
        self._load_error = None

    def _load_schedule_from_bytes(self, archive_bytes: bytes) -> None:
        """Publish a complete parsed snapshot, retaining the old one on failure."""
        candidate = StaticScheduleValidator(
            self._schedule_url,
            [(route, stop) for route in self._monitored_routes for stop in self._monitored_stops],
        )
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            candidate._parse_schedule(archive)
        for field in (
            "_route_ids", "_stop_ids", "_route_labels", "_route_stop_service_ids",
            "_departures_by_service", "_calendar", "_calendar_exceptions", "_agency_timezone",
        ):
            setattr(self, field, getattr(candidate, field))

    @staticmethod
    def _rows(archive, filename):
        """Read an optional GTFS table without retaining an open ZIP handle."""
        if filename not in archive.namelist():
            return
        with archive.open(filename) as table:
            yield from csv.DictReader(io.TextIOWrapper(table, "utf-8-sig"))

    def _parse_schedule(self, archive) -> None:
        if not {"calendar.txt", "calendar_dates.txt"}.intersection(archive.namelist()):
            raise ValueError("GTFS requires calendar.txt or calendar_dates.txt")
        for row in self._rows(archive, "agency.txt"):
            self._agency_timezone = ZoneInfo(row["agency_timezone"])
            break

        trip_to_route_service: dict[str, tuple[str, str]] = {}

        with archive.open("routes.txt") as route_file:
            for row in csv.DictReader(io.TextIOWrapper(route_file, "utf-8-sig")):
                route_id = row["route_id"]
                if route_id in self._monitored_routes:
                    self._route_ids.add(route_id)
                    self._route_labels[route_id] = (
                        row.get("route_short_name")
                        or row.get("route_long_name")
                        or route_id
                    )

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
                self._route_stop_service_ids[(route_id, stop_id)].add(service_id)
                departure_value = row.get("departure_time") or row.get("arrival_time")
                if not departure_value or row.get("pickup_type") == "1":
                    continue

                departure_seconds = parse_gtfs_seconds(departure_value)
                self._departures_by_service[(route_id, stop_id, service_id)].append(departure_seconds)

        for departure_list in self._departures_by_service.values():
            departure_list.sort()

        for row in self._rows(archive, "calendar.txt"):
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

        for row in self._rows(archive, "calendar_dates.txt"):
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
