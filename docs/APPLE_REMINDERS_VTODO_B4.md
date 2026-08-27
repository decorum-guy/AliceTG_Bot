# B4 Apple Reminders VTODO read probe

Status: `PENDING LIVE EVIDENCE`

Tracking: [artem-control-center#126](https://github.com/decorum-guy/artem-control-center/issues/126)

Base repositories inspected for this phase:

- `artem-control-center` main: `485a050cdc0703816a8140e8ead07f31b6450bbf`
- `AliceTG_Bot` main: `67d77307ff59dd71c1cc3f3ffc609cf409b2e5af`

## Scope

This phase probes whether the currently authorized Apple/CalDAV boundary can
discover and read Apple Reminders represented as iCalendar `VTODO` objects.
It does not integrate reminders into Control Center, mirror them into
Planning, create a second store, synchronize, or issue provider mutations.

Planning SQLite remains the canonical Control Center task/reminder store before,
during, and after B4, including if the live probe is positive.

## Existing architecture reused

The probe is [ICloudVTodoProbe](../app/planning/providers/icloud_vtodo_probe.py)
and reuses the accepted Alice CalDAV boundary:

- `ReadOnlyCalDavTransport`, whose only operations are `PROPFIND` and `REPORT`;
- `AiohttpCalDavTransport`, including HTTPS, TLS, Apple-host redirect, timeout,
  payload, and credential isolation controls;
- the existing property-scoped DAV discovery helpers and trusted href checks;
- the existing `icalendar` dependency (`7.2.2`) for in-memory VTODO parsing.

The probe is not wired into `ICloudCalDavProvider`, `ProviderCalendarCache`,
Planning API routes, browser code, or normal reminder responses. Parsed VTODOs
never enter `calendar_events` or SQLite.

## Read-only and bounded behavior

The probe performs the following layers:

1. authenticated `current-user-principal` discovery;
2. `calendar-home-set` discovery;
3. depth-1 collection enumeration with component, identity, freshness,
   privilege, sharing, and supported-report metadata;
4. selection of collections that either explicitly advertise `VTODO` or omit
   `supported-calendar-component-set` (RFC 4791 treats the latter as
   unrestricted accepted component types);
5. one bounded `calendar-query` `REPORT` per selected collection, requesting
   `getetag` plus partial `calendar-data` for a finite
   30-days-past/365-days-future UTC window;
6. strict in-memory VTODO inspection with no content returned in the result.

The component capability state is kept separate for every calendar collection:

- `explicit_vtodo_support`: the property exists and contains `VTODO`;
- `explicit_vtodo_exclusion`: the property exists and does not contain `VTODO`;
- `absent`: the property is absent, so standards semantics imply all component
  types are accepted, but no VTODO resource is inferred.

An absent property is therefore a safe read-probe candidate, not proof that
Apple Reminders exist. A successful empty query is still only a sampled
observation and is not proof that the collection is empty.

The live CLI uses the existing server-only variables
`PLANNING_ICLOUD_ACCOUNT`, `PLANNING_ICLOUD_PASSWORD`, and
`PLANNING_ICLOUD_CALDAV_URL`. It caps collection discovery at 32 collections,
each query at 128 response resources, and the transport response at 2 MiB.
It makes no request when any required variable is absent. The output contains
only counts, booleans, protocol property names, opaque IDs, and sanitized
provider error codes. It does not contain credentials, Authorization headers,
list names, reminder titles, notes, locations, URLs, or raw ICS/XML.

The exact command in the already authorized server environment is:

```text
./.venv/bin/python scripts/probe_icloud_vtodo.py
```

The three variables must be supplied by the existing server secret/configuration
path; they must not be placed in command-line arguments or pasted into chat.

## Verified from current live Apple path

No live Apple request was made in this agent environment. Boolean presence
checks found none of the three required server-side configuration variables;
their values were never read into output. Consequently, the current Apple
account's collection/resource behavior, identities, completion semantics,
freshness, sharing, and deletion behavior remain unverified.

This is intentionally not evidence that Apple Reminders is unsupported.

## Verified from deterministic fixtures

The probe test suite uses only synthetic account names, paths, list names,
resource data, and error responses. It verifies:

- VEVENT-only collections are not treated as VTODO collections based on a
  display name;
- explicit VTODO and mixed VEVENT/VTODO component advertisements are distinct;
- explicit VTODO, explicit VEVENT-only, absent-property empty-query,
  absent-property readable-resource, and absent-property query-failure
  collection states;
- empty, open, completed, date-only, TZID date-time, floating date-time, and
  no-due VTODO observations;
- RRULE, RECURRENCE-ID, EXDATE, and recurrence evidence; synthetic responses
  containing VALARM, RELATED-TO, priority, URL, location, categories, and
  undocumented X-properties verify sanitization and the `not_requested`
  advanced-property status;
- duplicate UID snapshot ambiguity and distinct collection identity for
  same-title items;
- identity fields, snapshot uniqueness, and unobserved longitudinal stability
  are separate evidence fields; `RECURRENCE-ID` is recorded separately because
  UID alone does not identify an occurrence;
- returned DAV resources, successfully parsed VTODO resources, parsed items,
  and parser failures are counted separately;
- href, UID, ETag, resource-id, sync-token, ctag, read-only, owner metadata,
  and supported-report observations;
- malformed XML/ICS, XML entity declarations, mixed resource payloads,
  duplicate hrefs, provider failures, and untrusted hosts;
- the transport call list contains only `PROPFIND` and `REPORT`, with no GET,
  PUT, POST, PATCH, DELETE, MKCALENDAR, MOVE, or COPY operation.

Current deterministic validation: `19` B4 tests passed; existing iCloud
Calendar provider validation: `29` tests passed.

Fixture behavior is not Apple-account evidence. It proves parser and boundary
behavior only.

## Query coverage and privacy decision

The final resource read is one `REPORT` per candidate collection. Its filter is
one finite `VTODO` `time-range` (30 days in the past through 365 days in the
future), and its response asks for `getetag` plus partial `calendar-data`.
Partial retrieval requests only `UID`, `DTSTAMP`, `LAST-MODIFIED`, `SEQUENCE`,
`DTSTART`, `DUE`, `DURATION`, `STATUS`, `COMPLETED`, `PERCENT-COMPLETE`,
`RRULE`, `RECURRENCE-ID`, `EXDATE`, and `RDATE` inside `VTODO`. The request
does not ask for `SUMMARY`, `DESCRIPTION`, `LOCATION`, `URL`, attachments,
attendees, or organizer data. If a server returns additional data, the probe
still emits only sanitized protocol presence/count facts. The structured
output marks the advanced-property group `not_requested`; it does not expose
`notesObserved`, `locationObserved`, `urlObserved`, alarm, tag, or other
advanced-property booleans whose false value could imply an observed absence.

This is deliberately a bounded sample, not representative enumeration. A
VTODO can be valid without a due date, and the RFC time-range rules depend on
the effective DTSTART/DUE/DURATION plus completion/creation and recurrence
semantics. The finite query can therefore miss undated/open items, old overdue
or completed items, and other values outside the window. A second unbounded
no-due query would increase history exposure and could still hit the resource
cap, so B4 does not use it. The structured result says
`coverage: sampled_not_enumerated` and
`zeroResultMeaning: not_observed_not_empty`; it never converts zero returned
resources into an empty-collection or unsupported verdict. Transport payload,
collection, and per-collection resource caps remain in force.

`vTodoQueryStatus` answers whether the VTODO query executed. Separately,
`vTodoResourceEvidence` is `observed` only when at least one returned DAV
resource contains successfully parsed VTODO data; it is `not_observed` for a
successful empty query, `failed` when returned resources all fail parsing, and
`partial` when parsed data is mixed with parser or collection-query failures.
`resourcesActuallyObserved`, `parsedVTodoResourcesActuallyObserved`,
`itemsActuallyObserved`, and `parserFailures` preserve the underlying counts.

## Standards/documentation support

- [RFC 4791, section 5.2.3](https://www.rfc-editor.org/rfc/rfc4791.html#section-5.2.3)
  defines `supported-calendar-component-set`, including `VEVENT` and `VTODO`.
  When present it restricts accepted component types; when absent the server
  must accept all component types and the client may assume they are accepted.
  Its absence does not prove that a provider exposes existing VTODO resources,
  so the probe queries such collections but does not infer Reminders from the
  missing property or a name.
- [RFC 4791, section 7.8](https://www.rfc-editor.org/rfc/rfc4791.html#section-7.8)
  defines `calendar-query`, its `DAV:multistatus` response, and finite
  component/time-range filtering. Section 7.6 defines partial retrieval of
  selected calendar components and properties. [Section 7.9](https://www.rfc-editor.org/rfc/rfc4791.html#section-7.9)
  defines `calendar-multiget`; the existing accepted provider uses it for
  resource verification, while B4 does not need it for the bounded initial
  read.
- [RFC 5545, section 3.6.2](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.6.2)
  defines VTODO, including valid no-due tasks. Its status, completion, due,
  recurrence, and related properties are protocol observations, not a license
  to invent an Apple-specific mapping.
- [RFC 6578](https://www.rfc-editor.org/rfc/rfc6578.html) defines WebDAV
  collection synchronization and `DAV:sync-token`; observing a token is not
  treated as proof that VTODO incremental/deletion semantics are usable until
  a live before/after observation establishes that.
- [RFC 4918, section 9.1](https://www.rfc-editor.org/rfc/rfc4918.html#section-9.1)
  describes `PROPFIND` as safe and idempotent. The existing transport enforces
  the narrower B4 method boundary in code rather than exposing generic HTTP.
- Apple's [Reminders account guide](https://support.apple.com/guide/iphone/add-or-remove-accounts-iph8739025dd/26/ios/)
  documents iCloud and other CalDAV accounts at the product level, but does
  not establish the current authorized iCloud CalDAV endpoint's VTODO
  collection/resource behavior. That behavior must come from the bounded live
  probe, not from the existence of the product feature or synthetic fixtures.

## Capability matrix

`Current Apple path evidence` is intentionally marked `NOT OBSERVED` until the
exact CLI is run through the existing authorized server configuration. Fixture
results are protocol/parser evidence only, never current-account evidence.

| Capability | Standards expectation | Current Apple path evidence | Product status | Notes |
|---|---|---|---|---|
| VTODO collection discovery | Present component-set may explicitly include/exclude VTODO; absent means standards-unrestricted acceptance | NOT OBSERVED | PENDING LIVE EVIDENCE | Absent property is queried but does not prove a VTODO resource exists |
| VTODO query capability | `calendar-query` can return matching calendar data | NOT OBSERVED | PENDING LIVE EVIDENCE | `vTodoQueryStatus` reports query execution separately from resource evidence |
| Actual VTODO resource evidence | A returned resource with valid VTODO data is readable evidence | NOT OBSERVED | PENDING LIVE EVIDENCE | `vTodoResourceEvidence` requires parsed VTODO data; zero results mean NOT OBSERVED, not empty |
| Collection identity fields | href identifies the DAV resource; resource-id may exist | NOT OBSERVED | PENDING LIVE EVIDENCE | Opaque ID is derived only after live href validation |
| Collection snapshot uniqueness | Duplicate collection hrefs fail closed | NOT OBSERVED | PENDING LIVE EVIDENCE | Snapshot uniqueness is not longitudinal stability |
| Collection longitudinal stability | Requires a later non-mutating provider observation | NOT OBSERVED | NOT TESTABLE SAFELY | One-shot B4 does not manufacture list rename/move/update evidence |
| Item identity fields | UID, resource href, and ETag may be returned | NOT OBSERVED | PENDING LIVE EVIDENCE | Values are not emitted; never match by title/due/note |
| Item snapshot uniqueness | UID within a collection is checked for ambiguity in this response | NOT OBSERVED | PENDING LIVE EVIDENCE | Duplicate UID is ambiguous even when resource hrefs differ |
| Item longitudinal stability | Requires later provider reads across an ordinary update | NOT OBSERVED | NOT TESTABLE SAFELY | No reminder or list is modified for B4 |
| ETag | WebDAV entity tag may provide revision evidence | NOT OBSERVED | PENDING LIVE EVIDENCE | Presence is recorded; value is not emitted |
| Revision/sync token | WebDAV sync-token may support collection synchronization | NOT OBSERVED | PENDING LIVE EVIDENCE | No sync report is run by B4 |
| Open/completed state | RFC status/completion properties can be present | NOT OBSERVED | PENDING LIVE EVIDENCE | Mapping requires live evidence |
| Cancelled state | RFC STATUS may be CANCELLED | NOT OBSERVED | PENDING LIVE EVIDENCE | No product mapping is assumed |
| Due DATE | RFC supports date-valued DUE | NOT OBSERVED | PENDING LIVE EVIDENCE | Date-only remains date-only |
| Due DATE-TIME | RFC supports date-time-valued DUE | NOT OBSERVED | PENDING LIVE EVIDENCE | UTC/TZID/floating are distinguished |
| Timezone | iCalendar parameters/value types define timezone semantics | NOT OBSERVED | PENDING LIVE EVIDENCE | No silent midnight-UTC coercion |
| No-due items | RFC allows VTODO without DTSTART and DUE/DURATION | NOT OBSERVED | PENDING LIVE EVIDENCE | Fixture parser supports it; finite query may not return it |
| Recurrence | RFC supports RRULE/RDATE/EXDATE and recurrence identifiers | NOT OBSERVED | PENDING LIVE EVIDENCE | Probe records presence; does not expand |
| Recurrence exceptions | `RECURRENCE-ID` identifies overridden instances | NOT OBSERVED | PENDING LIVE EVIDENCE | UID alone is not occurrence identity; series/occurrence completion remains unknown |
| Deletion detection | Sync reports or subsequent absence can provide evidence | NOT OBSERVED | NOT TESTABLE SAFELY | No reminder is deleted for B4 |
| Owner metadata | `DAV:owner` may be exposed on an ordinary owned collection | NOT OBSERVED | PENDING LIVE EVIDENCE | Reported as `ownerMetadataObserved`, never as shared-list evidence |
| Shared lists | Requires a sharing-specific signal not present in this probe | NOT OBSERVED | NOT OBSERVED | `sharedListStatus` and owner-vs-participant remain `not_observed` |
| Read-only privileges | DAV privilege set can expose read/write capabilities | NOT OBSERVED | PENDING LIVE EVIDENCE | Probe never uses write privilege |
| Malformed-resource handling | Provider must return bounded/sanitized failure | NOT OBSERVED | SUPPORTED IN FIXTURES | Live provider error behavior remains unknown |
| Rate-limit/error behavior | HTTP/DAV errors are provider-specific | NOT OBSERVED | SUPPORTED IN FIXTURES | Live status/rate limits remain unknown |
| Credential isolation | Server-side auth is outside browser boundary | NOT OBSERVED | SUPPORTED BY EXISTING BOUNDARY | No browser CalDAV route exists |
| Browser isolation | No arbitrary URL/method supplied by browser | NOT OBSERVED | SUPPORTED BY EXISTING BOUNDARY | URL is server configuration only |

## Inferences / product interpretation

The code and synthetic fixtures establish that a future live result can be
collected without widening the existing provider trust boundary or entering
Planning storage. They do not establish that the current Apple account exposes
VTODO data.

If the live run finds only explicitly excluded collections, that component
advertisement is evidence against VTODO acceptance. If relevant collections
omit `supported-calendar-component-set`, however, absence of explicit VTODO
advertisement is not option 3: those collections are standards-unrestricted
and must be queried. A successful finite query with zero resources is also only
`not_observed`, not proof of an empty collection. If the live run finds VTODO
resources but identity, freshness, recurrence, deletion, sharing, or time
semantics are ambiguous, that supports option 2. Longitudinal stability remains
unobserved unless safe pre-existing evidence establishes it; B4 does not create
an update to test it. If all required semantics are sufficiently stable, that
may support option 1 and a separate future implementation issue; it still would
not authorize integration in B4.

## Unknown / not safely testable

Until a live run is available, the following are unknown:

- whether Apple exposes any VTODO-capable collection on this account;
- whether a VTODO `calendar-query` succeeds through this accepted endpoint;
- whether the bounded sample returns any successfully parsed VTODO resource;
- whether UID/href/ETag remain stable across ordinary remote updates;
- whether Apple exposes recurrence exceptions and completion per occurrence in
  a usable form;
- whether sync-token/ctag behavior can establish VTODO deletion/tombstones;
- whether shared lists expose owner/participant and privilege distinctions;
- Apple-specific flags, tags, locations, alarms, subtasks, and private X-
  properties; these are intentionally not requested by the B4 partial read;
- account-specific HTTP rate limits and partial-response behavior.

## Product verdict

`PENDING LIVE EVIDENCE` — no option 1/2/3 is selected in this checkout because
the authorized live credential was unavailable, as required by issue #126's
evidence rule. The exact bounded command above is ready for execution through
the existing trusted environment. No Control Center code or UI was changed,
and no production deployment or Apple mutation was performed.
