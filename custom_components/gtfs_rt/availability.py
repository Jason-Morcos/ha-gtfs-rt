from __future__ import annotations

from .health import STATUS_LOOKUP_FAILED, ScheduleStatus


def should_mark_entity_unavailable(
    *,
    last_trip_update_error: str | None,
    schedule_status: ScheduleStatus | None,
    has_realtime_departures: bool,
) -> bool:
    """Return whether the entity should be marked unavailable."""
    if last_trip_update_error:
        return True
    if schedule_status is None or schedule_status.status == STATUS_LOOKUP_FAILED:
        return False
    if schedule_status.is_config_problem:
        return True
    # Some agencies publish realtime trip updates with truncated stop lists. When
    # the route/stop pair is valid in the static schedule, keep the entity
    # available so the state can remain unknown instead of looking broken.
    if schedule_status.service_expected_now and not has_realtime_departures:
        return False
    return False
