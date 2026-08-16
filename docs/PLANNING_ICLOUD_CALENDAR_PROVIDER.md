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

The concrete transport has only `PROPFIND` and `REPORT` methods. There is no
CalDAV `PUT`, `DELETE`, event creation/update/move, calendar mutation, invite
response, or writeback method in the adapter surface. Fixture transports assert
that every call is `PROPFIND` or `REPORT`.

Discovery follows CalDAV `current-user-principal` and `calendar-home-set`
properties, then discovers calendar collections and calendar data. It does not
depend on an observed iCloud shard hostname.

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
two calendars with the same name remain distinct. Raw CalDAV hrefs are kept
only in the live adapter transport and are never stored or returned.

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

Migration 005 adds three provider-only tables:

- `provider_sources`: safe account identity, enabled/configured/status,
  timestamps and sanitized error code;
- `provider_calendars`: opaque calendar identity, display name/color, status and
  freshness;
- `provider_event_cache`: canonical UUIDv4 mapping, opaque event identity,
  recurrence identity, bounded window and refresh marker.

The trusted cache path writes provider-owned rows directly. It never calls
`EventService.create/update/delete`, so native local Planning ownership and its
local-only mutation guard are unchanged. Native events and imported events
coexist in the existing provider-neutral calendar range and read-by-ID API.

The server-owned refresh loop uses a rolling finite window of 30 days in the
past through 365 days in the future, refreshed every five minutes by default.
No entire-history download is performed. All calendar/event counts and payload
sizes are bounded.

A complete successful refresh atomically updates the source, calendars, event
rows and mapping rows. A provider timeout, authentication failure, malformed
payload or partial refresh leaves the prior cache intact, marks the provider
source/cache stale or error, and never tombstones from incomplete information.
A later successful authoritative refresh of the same complete window may
tombstone only provider occurrences conclusively absent from that window. The
tombstone is local cache state, not an external delete, and preserves the
provider identity for read-by-ID diagnostics.

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

The cache tables are included in the existing encrypted Planning SQLite backup
because they are part of the durable database snapshot, but they are
reconstructable and may be stale after restore. The next successful provider
refresh replaces them. No credential is stored in the tables or backup
manifest. Existing native Planning data is not rewritten.

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
