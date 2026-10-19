"""Credential-safe GTFS feed decoding, adapted from upstream v1.0.0 (MIT)."""

from __future__ import annotations

from dataclasses import dataclass

import requests
from google.protobuf import json_format
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from .const import REQUEST_TIMEOUT
from .realtime import valid_position

ACCEPT = "application/x-protobuf, application/octet-stream;q=0.9, */*;q=0.8"


class FeedFetchError(Exception):
    """A feed could not be downloaded; messages never include request data."""


class FeedAuthError(FeedFetchError):
    """The remote server rejected credentials."""


class InvalidFeedError(FeedFetchError):
    """A response was not a GTFS-realtime feed."""


def fetch_feed(url, headers):
    """Download and decode a feed. Call only from an executor."""
    request_headers = dict(headers or {})
    if not any(key.lower() == "accept" for key in request_headers):
        request_headers["Accept"] = ACCEPT
    response = None
    try:
        response = requests.get(url, headers=request_headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except Exception as err:
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status in (401, 403):
            raise FeedAuthError(f"HTTP {status}: check feed credentials") from None
        if isinstance(status, int) and status >= 400:
            raise FeedFetchError(f"HTTP {status}") from None
        raise FeedFetchError(type(err).__name__) from None
    return parse_feed(response.content)


def parse_feed(content: bytes) -> gtfs_realtime_pb2.FeedMessage:
    """Parse a protobuf (or JSON) GTFS-realtime feed."""
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except DecodeError as err:
        # Only fall back to JSON after binary fails: a binary feed starts with
        # 0x0A ("\n") and can be followed by "{", so sniffing would misfire.
        if content.lstrip()[:1] != b"{":
            raise InvalidFeedError(_describe(content)) from err
        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            json_format.Parse(content, feed, ignore_unknown_fields=True)
        except (json_format.ParseError, UnicodeDecodeError) as json_err:
            raise InvalidFeedError(_describe(content)) from json_err
    if not feed.HasField("header") and not feed.entity:
        raise InvalidFeedError(_describe(content))
    return feed


def _describe(content: bytes) -> str:
    """Explain why a response is not a GTFS-realtime feed."""
    head = content.lstrip()[:64]
    if not head:
        return "the server returned an empty response"
    if head.startswith(b"PK"):
        return (
            "the server returned a ZIP file; this looks like a static GTFS "
            "schedule rather than a GTFS-realtime feed"
        )
    if head.startswith(b"<"):
        return (
            "the server returned an HTML or XML page instead of a feed; "
            "check the URL and credentials"
        )
    if head.startswith(b"{"):
        return "the server returned JSON that is not a GTFS-realtime feed"
    if all(32 <= byte < 127 or byte in b"\t\r\n" for byte in head):
        return (
            "the server returned text instead of binary protobuf; remove any "
            "debug or text-format option from the URL"
        )
    return "the response could not be parsed as a GTFS-realtime feed"


@dataclass(frozen=True)
class VehicleDetails:
    vehicle_id: str | None
    route_id: str
    position: object | None
    occupancy: str | None


class VehicleIndex(dict):
    """Retain the legacy trip map while distinguishing frequency instances."""

    def __init__(self, feed):
        super().__init__()
        self._by_trip = {}
        self._by_trip_id = {}
        for entity in feed.entity:
            if entity.is_deleted or not entity.HasField("vehicle"):
                continue
            row = entity.vehicle
            occupancy = None
            if row.HasField("occupancy_status"):
                try:
                    occupancy = gtfs_realtime_pb2.VehiclePosition.OccupancyStatus.Name(
                        row.occupancy_status
                    )
                except ValueError:
                    pass
            value = VehicleDetails(
                row.vehicle.id or None,
                row.trip.route_id,
                valid_position(
                    row.position.latitude
                    if row.position.HasField("latitude")
                    else None,
                    row.position.longitude
                    if row.position.HasField("longitude")
                    else None,
                ),
                occupancy,
            )
            if not row.trip.trip_id:
                continue
            key = trip_key(row.trip)
            previous = self._by_trip.get(key, value)
            self._by_trip[key] = value if previous == value else None
            previous = self._by_trip_id.get(row.trip.trip_id, (key, value))
            self._by_trip_id[row.trip.trip_id] = (
                (key, value) if previous == (key, value) else None
            )
        for trip_id, candidate in self._by_trip_id.items():
            if candidate and candidate[1].vehicle_id:
                self[trip_id] = candidate[1].vehicle_id

    def for_trip(self, trip):
        if not trip.trip_id:
            return None
        key = trip_key(trip)
        if key in self._by_trip:
            return self._by_trip[key]
        candidate = self._by_trip_id.get(trip.trip_id)
        if candidate is None:
            return None
        observed_key, value = candidate
        # A known conflicting service instance cannot lend position or route.
        if any(
            expected and observed and expected != observed
            for expected, observed in zip(key[1:], observed_key[1:])
        ):
            return None
        return value


def trip_key(trip):
    return trip.trip_id, trip.start_date, trip.start_time
