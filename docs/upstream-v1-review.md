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

## Second-pass interaction review

The follow-up review reproduced and corrected additional interactions:

- Keep original provider reports internally across cache/stop-wide merges.
  Otherwise a preferred provider without frequency metadata can erase that
  evidence, or an unprefixed ID can bridge distinct agencies. All reports in a
  duplicate group must agree; time-only groups cannot chain past their window.
  Re-expanded reports are checked for expiry again before sensors/caches use them.
- Treat cleared optional form fields as removed, including values omitted by the
  Home Assistant frontend. Verify actual URL/header reloads while preserving IDs.
- Prevent YAML imports from taking over UI-owned feeds or reusing another feed's
  entity IDs, while preserving intentional distinct YAML namespaces for one URL
  and normal migration of this integration's unowned legacy registry identities.
- Evaluate primary failure and fallback coverage for each configured departure.
  Preserve known failures for uncovered routes during partial Transit fallback
  or OneBusAway backoff. API-level failures retain successful earlier stop results. A healthy empty primary feed is not broken by an optional
  provider outage; a freshly successful empty mapped fallback remains healthy.
- Validate JSON envelopes and nested optional provider structures independently.
  Malformed optional metadata cannot discard neighboring healthy predictions.
  Missing, nonfinite or out-of-range positions stay unknown; explicit zero
  occupancy remains present.
- Collect observed route IDs inside all-routes provider buckets for diagnostics,
  deduplicate repeated reports in counts, and expose scoped source failures.

Regression coverage is in `tests/test_second_pass.py`,
`tests_ha/test_lifecycle_regressions.py`, and
`tests_ha/test_provider_regressions.py`. All fixtures are synthetic. This evidence
remains source/test evidence; release, installation and live provider verification
remain separate.


## Broader fork audit

The additional fork review reproduced and repaired these independent defects:

- A failed OneBusAway stop during initial cache warmup prevented later stops
  from being fetched. Continue independent stop requests after ordinary failures;
  retain the server-wide stop on HTTP 429 and merge healthy results with fallback.
  Track warmup attempts separately from successful refreshes so persistent
  failures cannot keep triggering multi-stop startup bursts.
- Use OneBusAway departure times while a vehicle is boarding, calculate delay
  against the matching scheduled event, and omit explicit dropoff-only records.
- Keep separate visits to a stop on a loop trip. Stop-sequence evidence remains
  provider-specific: OneBusAway uses zero-based indices, unlike GTFS sequences.
  Ambiguous unindexed reports cannot bridge distinct known visits.
- Publish each successful Transit batch even if another batch fails. Preserve
  unexpired failed-batch cache, clear successful empty results, and scope fresh
  empty-source coverage to the actual successful stops. Ordinary failures do not
  starve later batches; HTTP 429 stops further requests and retains backoff.
- A boardable untimed intermediate stop has service on its active calendar days,
  even though its precise departure remains unknown.
- Static frequency templates are offsets rather than literal departures.
  Compressed exact schedules calculate the next run arithmetically; non-exact
  frequencies expose service windows without fabricating departure timestamps.
  Both retain agency-timezone, calendar, carryover, and snapshot guarantees.

The semantics follow the [GTFS schedule reference](https://gtfs.org/documentation/schedule/reference/#frequenciestxt)
and [OneBusAway arrival/departure reference](https://developer.onebusaway.org/api/where/elements/arrival-and-departure).
Regression coverage is in `tests/test_fork_audit.py`,
`tests/test_schedule_frequencies.py`, and `tests_ha/test_provider_regressions.py`.
All new fixtures use synthetic stops, routes and credentials.

Read-only Home Assistant background checks confirmed HACS v1.5.2 installed on
HA 2026.10.1 with fresh entity reports. Bounded historical logs included a
stop-arrivals connection failure with fallback and a slow-update warning.
Those logs establish operational context, not that the synthetic bugs caused
that incident or that this PR is installed. No live configuration, log level,
restart, reload or integration installation was changed.


## GitHub Codex review corrections

The full GitHub review of PR #16 at `1df5ba3` identified two additional interactions.
Stop-wide trip-ID matching now rejects known conflicting routes even across
providers, while preserving equivalent prefixed/unprefixed route IDs and
known agency boundaries. OneBusAway delay and scheduled matching metadata use
only the scheduled event corresponding to the selected realtime event; missing
counterpart timestamps remain unknown instead of adding dwell time to delay.
Both findings have synthetic regressions, including reversed/repeated merge
orders and both arrival/departure mismatch directions.


The next full Codex review at `b74932f` found three additional defects:

- Vehicle enrichment now considers the current TripUpdates as well as vehicle
  records. A vehicle without enough service-instance metadata cannot supply
  route, position or occupancy to several known frequency instances.
- Feed decoding requires the mandatory header and initialized protobuf fields.
  Valid header-only empty feeds and actionable safe payload errors remain
  supported; structurally incomplete sidecars fail in isolation from arrivals.
- `Service today` uses actual agency civil-day overlap for timed departures and
  both frequency modes. A Friday service-day trip running only after midnight
  belongs to Saturday's displayed service health. Calendar exceptions, elapsed
  DST times, carryover, upcoming instants and untimed-calendar health remain.

Regressions include binary/JSON/header-only feed cases, native UI rejection,
known/unknown frequency vehicle metadata, and service-day versus civil-day cases.
