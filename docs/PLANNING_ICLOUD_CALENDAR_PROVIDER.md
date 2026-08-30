# Planning iCloud Calendar Provider (Phase A)

This phase adds a server-only, read-only iCloud/CalDAV adapter and a bounded
SQLite cache. It does not change Control Center and it does not enable any
provider write path.

## Trust boundary

Alice owns the iCloud account and app-specific password. The values are read
only from server runtime configuration:

```text
PLANNING_ICLOUD_ENABLED=false
PLANNING_ICLOUD_ACCOUNT=<runtime secret reference/value>
PLANNING_ICLOUD_PASSWORD=<runtime secret>
PLANNING_ICLOUD_CALDAV_URL=<server-only HTTPS bootstrap URL>
PLANNING_ICLOUD_REFRESH_INTERVAL_SECONDS=300
```

The provider is disabled by default. Missing credentials or bootstrap URL are
represented as `not_configured`; they do not start a retry/crash loop. No
credential is placed in Planning objects, cache tables, logs, API envelopes,
source references, backups' manifest, CI configuration, or frontend variables.

The CalDAV bootstrap URL is server-owned configuration, never a browser input.
The transport requires HTTPS, verifies TLS, bounds connect/read/total time,
limits payload size and redirects, and accepts redirects only to the original
host or Apple-owned `*.icloud.com`/`*.apple.com` hosts. URLs containing userinfo
are rejected.

## Read-only contract

`ExternalCalendarProvider` exposes only:

- `discover_account()`;
- `list_calendars()`;
- `fetch_events(calendar, window)`.

The iCloud adapter also implements a separate optional read-only
`verify_resources(calendar, resource_refs, window)` capability for bounded
authoritative reconciliation. It has no mutation methods and does not widen
the provider-neutral fetch contract.

The concrete transport has only `PROPFIND` and `REPORT` methods. There is no
CalDAV `PUT`, `DELETE`, event creation/update/move, calendar mutation, invite
response, or writeback method in the adapter surface. Fixture transports assert
that every call is `PROPFIND` or `REPORT`.

Discovery follows CalDAV `current-user-principal` and `calendar-home-set`
properties, then discovers calendar collections and calendar data. The DAV
parser is property-scoped: it accepts only a successful propstat's nested
`DAV:href` inside the requested property, never the enclosing response href.
`DAV:unauthenticated`, missing properties, ambiguous values, and non-2xx
propstats fail closed with sanitized codes. It does not depend on an observed
iCloud shard hostname.

`icalendar==7.2.2` parses iCalendar and timezone values. The maintained
`recurring-ical-events==3.8.2` library expands occurrences for the finite
requested window and applies recurrence exceptions/`RECURRENCE-ID` semantics.
Planning local recurrence editing remains disabled.

## Identity

The account ID is a one-way SHA-256-derived opaque value over the normalized
account name and is displayed only as an opaque ID. The display label is
`iCloud`.

Calendar IDs are one-way opaque values over account identity plus the canonical
discovered calendar reference. Calendar display names are not identifiers, so
two calendars with the same name remain distinct. A trusted resource href may
be retained only in the internal provider cache for read-only deletion
verification; it is never a browser-facing identity, `CalendarEvent.source_ref`,
health/API metadata, log field, or audit field.

Event IDs are canonical Alice UUIDv4 values. SQLite maps the opaque provider
identity `(calendar identity, iCloud UID, recurrence-instance identity)` to the
UUID. A changed title or time for the same non-recurring UID keeps its UUID;
recurrence instances have separate stable identities. Provider event and
calendar fields in `CalendarEvent` are opaque hashes, and `source_ref` contains
only an opaque event mapping.

## Normalization

Timed events are returned as:

```text
all_day=false
timezone=<validated IANA zone>
start_at_utc=<UTC>
end_at_utc=<UTC>
start_date=null
end_date_exclusive=null
```

The semantic `TZID`, a zoneinfo key, or calendar `X-WR-TIMEZONE` is used. A
floating value uses the explicit configured Planning timezone, not the host
machine timezone. An unknown timezone fails the object/source with a stable
sanitized error code.

All-day events retain date semantics:

```text
all_day=true
timezone=<semantic IANA zone>
start_date=D
end_date_exclusive=D+1 day (for a one-day event)
start_at_utc=null
end_at_utc=null
```

They are never converted into midnight timed events.

Titles, notes and locations remain literal bounded data for the owner-facing
Planning API. They are not written to logs. ICS bodies, attendees, organizer
data, conference URLs, alarms and attachments are not logged or projected.

## Cache and reconciliation

Migration 005 adds three provider-only tables, and migration 006 adds the
durable deletion-confirmation marker:

- `provider_sources`: safe account identity, enabled/configured/status,
  timestamps and sanitized error code;
- `provider_calendars`: opaque calendar identity, display name/color, status and
  freshness;
- `provider_event_cache`: canonical UUIDv4 mapping, opaque event identity,
  recurrence identity, bounded window, refresh marker, an optional server-only
  trusted resource reference, and persisted successful-miss confirmation
  state. The confirmation state is separate from generic provider staleness.

The trusted cache path writes provider-owned rows directly. It never calls
`EventService.create/update/delete`, so native local Planning ownership and its
local-only mutation guard are unchanged. Native events and imported events
coexist in the existing provider-neutral calendar range and read-by-ID API.

The server-owned refresh loop uses a rolling finite window of 30 days in the
past through 365 days in the future, refreshed every five minutes by default.
No entire-history download is performed. All calendar/event counts and payload
sizes are bounded.

The current production bootstrap creates one refresh-loop task for the one
provider cache. Each loop iteration awaits `cache.refresh()` before sleeping;
status and read routes do not trigger another refresh. Therefore refreshes do
not overlap in the current single-process architecture, and the cache's one
transaction per refresh remains the atomic state boundary. Any future manual
refresh trigger must preserve this serialization rather than adding a broad
application-wide lock.

A complete successful refresh atomically updates the source, calendars, event
rows and mapping rows. A provider timeout, authentication failure, malformed
payload or partial refresh leaves the prior cache intact, marks the provider
source/cache stale or error, and never tombstones from incomplete information.
A shifted rolling window is reconciled conservatively. For cached resources
that are absent from the new time-range response, the adapter issues a bounded
CalDAV `calendar-multiget` `REPORT` using the trusted resource href. An explicit
successful resource read keeps the event (and marks the old cached occurrence
stale), including when the event moved outside the requested window. The
multiget parser accepts both RFC-style response-level `DAV:status` and the
existing `propstat` status path. An explicit 404/410 for that exact resource is
a deletion candidate only: the first complete successful missing observation
is persisted without tombstoning, and a second consecutive complete successful
missing observation confirms the deletion and creates one tombstone. Thus a
single transient provider inconsistency cannot make a canonical event
disappear, while a real deletion is eventually reflected deterministically.

Data fetched by the current calendar query always wins over a contradictory
missing verification for the same event/resource in that refresh. Omitted or
duplicate requested hrefs, direct 200 without calendar data, and other non-2xx
statuses fail closed. Provider timeouts, authentication failures, malformed or
incomplete payloads, partial refreshes, and disappeared calendars do not count
as deletion evidence; provider failure also clears pending deletion evidence.
Pending confirmation is stored in the provider cache and survives process
restart. Repeated confirmed misses do not change the tombstone again, and a
later valid provider observation restores the same canonical event identity.
No CalDAV `DELETE` is issued.

## Transport failure categories

`aiohttp==3.13.2` failures are persisted only as fixed, sanitized provider
codes. HTTP 401/403 are `provider_authentication_failed`; 429 is
`provider_rate_limited`; 5xx is `provider_server_failure`; other non-success
HTTP responses are `provider_read_failed`. Connection and socket timeout
classes remain distinct as `provider_connection_timeout` and
`provider_read_timeout`; an otherwise indistinguishable total timeout is
`provider_timeout`.

The transport also distinguishes `provider_dns_failed`,
`provider_connection_refused`, `provider_connection_failed`,
`provider_tls_failed`, `provider_connection_reset`,
`provider_connection_aborted`, and `provider_server_disconnected` when the
pinned aiohttp class or OS errno proves that condition. Other aiohttp client
errors become `provider_transport_unknown`. These categories do not prove an
Apple service cause, network path, hostname, URL, account, response body, or
raw exception detail.

Existing fixed redirect (`provider_redirect_invalid`,
`provider_redirect_untrusted`, `provider_redirect_limit`) and payload/protocol
codes remain distinct. The latest category is retained in the existing source
and calendar `errorCode` fields and is cleared by a complete successful
refresh.

Provider data freshness is intentionally separate from the latest refresh
attempt. The stale threshold is `max(600 seconds, 2 * configured iCloud refresh
interval)`; the default 300-second refresh interval therefore retains data as
current for 600 seconds after its last successful sync. During that window a
failed attempt records its fixed `errorCode` while source/calendar status stays
`current` and cached events stay `synced`. Once the persisted last-success age
reaches the threshold, a failed attempt marks retained provider data stale.
Malformed or missing last-success timestamps fail closed to stale; a backwards
clock movement of at most 60 seconds is treated as zero age for a small
NTP/system-clock correction, while a larger backwards movement fails closed to
stale. This slice adds no proxy, retry/backoff, or refresh-cadence change, and
preserves the read-only CalDAV boundary.

A calendar-list refresh that no longer returns a previously known calendar
marks that calendar `disabled` with `provider_calendar_disappeared` and marks
its cached events `stale`; it does not falsely delete their provider objects.
Tombstones preserve provider identity for read-by-ID diagnostics.

Before calling `recurring-ical-events`, the adapter computes a conservative
upper bound from each RRULE's frequency, interval, COUNT/UNTIL/window span and
BY-part multiplicity, plus RDATE values. If the bound exceeds the per-calendar
event cap, it returns `provider_event_limit` without invoking the expansion
library. The pinned library remains responsible for actual recurrence and
exception/`RECURRENCE-ID` semantics.

## Freshness and health

`planning.v1` remains additive. When the provider cache is attached, envelopes
include a `sources` array alongside the existing `sourceStatus`,
`lastSyncedAt`, and `staleAfter` fields. It contains a current native Planning
source and the iCloud source with safe account/calendar metadata. Provider
staleness/error is therefore not collapsed into native Planning freshness.

The operational health snapshot uses `disabled`, `not_configured`, `current`,
`stale`, or `error` from the cached source state. Status polling reads SQLite
only and never contacts iCloud.

Current provider events use `sync_state=synced`. Cached rows during an outage
use `sync_state=stale`; native local rows remain `local_only` and remain
current. An imported event always has truthful provider identity and cannot be
patched or deleted through the B4 local mutation API; those calls return
`409 event_not_local_only`.

## Backup policy

The cache tables, including the internal resource reference used for
verification, are included in the existing encrypted Planning SQLite backup
because they are part of the durable database snapshot, but they are
reconstructable and may be stale after restore. The next successful provider
refresh replaces them. No credential is stored in the tables or backup
manifest, and the internal reference is never exposed by the API or health
response. Existing native Planning data is not rewritten.

## Runtime activation after merge

1. Provision an Apple app-specific password through the deployment secret
   manager; do not paste it into chat or commit it.
2. Set the four server-only iCloud variables and keep the rollout flag off
   until the runtime configuration is reviewed.
3. Enable the provider, observe the cached source health and source metadata,
   then verify Today/Agenda/day/week ranges against the authoritative iCloud
   calendar.

There is intentionally no iCloud-specific browser endpoint, arbitrary CalDAV
proxy, Panel Agent credential, or provider mutation capability in this phase.
