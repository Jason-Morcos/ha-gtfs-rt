# Upstream v1.0.0 review

Reviewed 2026-10-18 against fork base `41970e5` and upstream release commit
`5ffa9de88e2e079d88a0bb27befa40037b2a367b`.

Sources: [release notes](https://github.com/zacs/ha-gtfs-rt/releases/tag/v1.0.0),
[rewrite PR #42](https://github.com/zacs/ha-gtfs-rt/pull/42), and the release's
`feed.py`, `coordinator.py`, `config_flow.py`, `sensor.py`, `diagnostics.py`, tests
and CI configuration. Upstream is MIT-licensed under the same LICENSE.md retained
by this fork. Feed decoding/error descriptions are adapted from that release.

## Decisions

| Upstream improvement | Fork decision and resulting behavior |
| --- | --- |
| UI feed and departure setup | Add UI creation, repeatable departures, add/remove options and feed reconfiguration to the existing config-entry format. Keep stable feed/departure IDs and existing route devices. Reserve simultaneous setup URLs and reject entity-ID collisions across feeds and legacy YAML. Imported entries stay YAML-managed. |
| Per-departure config subentries/devices | Keep the fork's route grouping and configured data model. Route-free departures get stop devices. No forced device migration or loss of area assignments. |
| Optional route | Support stop-wide departures consistently across GTFS-RT, OneBusAway, Transit and static service validation. Keep route-specific configuration and identities unchanged. |
| Ten upcoming departures | Expand the existing richer `Upcoming departures` list from five to ten, retaining its field names, delay and provider provenance. Do not duplicate it with a second attribute schema. |
| YAML unique IDs | Already supported. Preserve explicit IDs and the existing derived-ID algorithm; feed reconfiguration retains the stored feed ID. New UI feeds allocate an independent stable feed identity; reusing a URL after rotation cannot alias another feed's devices. |
| JSON GTFS-RT | Decode protobuf first, then JSON after binary failure; accept camelCase/snake_case and future JSON fields. A binary header may itself begin with a newline and brace. |
| Departure-only events | Already supported. Preserve departure precedence when both events are present; a past departure must not be resurrected by a contradictory future arrival. |
| Missing route ID | Infer only from a position associated with the same trip instance, including vehicles without IDs. Never borrow the route of an unrelated trip using the same vehicle. |
| Invalid-feed explanations | Reject empty HTTP bodies, ZIP schedules, HTML/XML and debug text with fixed, credential-safe messages. Valid header-only feeds remain healthy. Add default protobuf Accept negotiation unless the caller supplied Accept. |
| Redacted diagnostics | Add explicit safe projection with observed routes per configured stop and separate trip/vehicle error states. Omit credentials, headers and names; redact URL userinfo, path, query and fragment. |
| Timezones | Already uses UTC instants, HA-local display, DST-safe elapsed minutes and agency-timezone service days. Retain those tests. |
| Canceled/skipped/deleted data | Retain canceled trips, deleted entities, SKIPPED and NO_DATA filtering; also exclude the newer DELETED trip relationship and ID-less stops. Update bindings to 2.2.0. |
| Missing occupancy/position | Already remains unknown rather than EMPTY or 0°,0°. Keep explicit zero delay/occupancy distinct from missing data. |
| Vehicle feed isolation | Already isolated. Expose its error state and log outage/recovery transitions; valid trip updates continue during vehicle outages. |
| Slow feeds and modern async coordinator | Keep existing synchronous polling in Home Assistant's executor, including initial entity refresh and UI validation. Do not introduce blocking HTTP into the event loop or replace the fork's throttled multi-provider caches with a single-source coordinator. |
| Malformed timestamps | Already isolates invalid timestamps and malformed OneBusAway/Transit rows. Add real-binding regression coverage with a healthy neighboring trip. |
| Frequency-based service | Match vehicle enrichment by `(trip_id, start_date, start_time)`, refuse ambiguous/conflicting matches, and retain those fields through deduplication so separate instances are not collapsed. Missing metadata can join one compatible instance; multiple known instances remain ambiguous. Cross-provider ID prefixes and omitted service dates retain compatibility; distinct raw same-provider IDs and known conflicting agency prefixes remain separate. |
| Unknown/no-service state | Already uses unknown for healthy empty/no-service feeds and unavailable for failed realtime acquisition or proven invalid route/stop configuration. Keep schedule-aware health distinctions. |
| Multi-version CI | Add standalone plus real HA config-flow, registration, diagnostics and unload checks on minimum 2025.6 and current pinned HA test releases. Keep synthetic data and credentials. |

## Retained fork behavior

Transit preference, OneBusAway stop refresh pacing, API request batching,
Retry-After handling, cached-provider fallback, provider-aware deduplication,
static-feed retry/snapshot safety, extended service-day handling, and safe removal
of retired route devices remain owned by the fork. Household choices and live
provider credentials remain outside this public source.

The review and automated tests establish source behavior only. PR publication,
review/merge, release packaging, HACS installation, loaded runtime and live agency
behavior are separate boundaries. This change does not release or install a new
version.
