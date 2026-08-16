-- Read-only external calendar cache. It stores no credentials and no raw URLs.

CREATE TABLE provider_sources (
    source_id TEXT PRIMARY KEY CHECK (length(source_id) BETWEEN 1 AND 128),
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    account_id TEXT NOT NULL CHECK (length(account_id) BETWEEN 1 AND 128),
    display_label TEXT NOT NULL CHECK (length(display_label) BETWEEN 1 AND 100),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    configured INTEGER NOT NULL CHECK (configured IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('current', 'stale', 'error', 'not_configured', 'disabled')),
    last_successful_sync_at TEXT,
    observed_at TEXT NOT NULL,
    last_error_code TEXT CHECK (last_error_code IS NULL OR length(last_error_code) <= 128),
    updated_at TEXT NOT NULL
);

CREATE TABLE provider_calendars (
    provider_calendar_id TEXT PRIMARY KEY CHECK (length(provider_calendar_id) BETWEEN 1 AND 256),
    source_id TEXT NOT NULL REFERENCES provider_sources(source_id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    color TEXT CHECK (color IS NULL OR length(color) BETWEEN 7 AND 9),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('current', 'stale', 'error', 'not_configured', 'disabled')),
    last_successful_sync_at TEXT,
    observed_at TEXT NOT NULL,
    last_error_code TEXT CHECK (last_error_code IS NULL OR length(last_error_code) <= 128),
    updated_at TEXT NOT NULL
);

CREATE TABLE provider_event_cache (
    canonical_event_id TEXT PRIMARY KEY CHECK (length(canonical_event_id) = 36),
    source_id TEXT NOT NULL REFERENCES provider_sources(source_id) ON DELETE RESTRICT,
    provider_calendar_id TEXT NOT NULL REFERENCES provider_calendars(provider_calendar_id) ON DELETE RESTRICT,
    provider_event_id TEXT NOT NULL CHECK (length(provider_event_id) BETWEEN 1 AND 256),
    identity_key TEXT NOT NULL CHECK (length(identity_key) BETWEEN 1 AND 256),
    recurrence_instance_key TEXT NOT NULL CHECK (length(recurrence_instance_key) BETWEEN 1 AND 128),
    -- Server-only trusted CalDAV resource reference; never API-facing.
    resource_ref TEXT CHECK (resource_ref IS NULL OR length(resource_ref) BETWEEN 1 AND 512),
    window_start_utc TEXT NOT NULL,
    window_end_utc TEXT NOT NULL,
    last_seen_refresh TEXT NOT NULL CHECK (length(last_seen_refresh) = 36),
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, identity_key)
);

CREATE INDEX idx_provider_event_cache_range
    ON provider_event_cache (source_id, provider_calendar_id, window_start_utc, window_end_utc);

CREATE INDEX idx_provider_calendars_source
    ON provider_calendars (source_id, enabled, provider_calendar_id);
