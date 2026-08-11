CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    notes TEXT CHECK (notes IS NULL OR length(notes) <= 4000),
    source TEXT NOT NULL CHECK (source IN ('alice', 'telegram', 'panel-agent', 'operator', 'ticktick', 'calendar')),
    source_ref TEXT CHECK (source_ref IS NULL OR length(source_ref) <= 256),
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audit_correlation_id TEXT NOT NULL CHECK (length(audit_correlation_id) = 36),
    deleted_at TEXT
);

CREATE TABLE reminders (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    notes TEXT CHECK (notes IS NULL OR length(notes) <= 4000),
    due_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL CHECK (length(timezone) BETWEEN 1 AND 64),
    status TEXT NOT NULL CHECK (status IN ('pending', 'due', 'completed', 'cancelled')),
    source TEXT NOT NULL CHECK (source IN ('alice', 'telegram', 'panel-agent', 'operator', 'ticktick', 'calendar')),
    source_ref TEXT CHECK (source_ref IS NULL OR length(source_ref) <= 256),
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 128),
    completed_at TEXT,
    cancelled_at TEXT,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('not_due', 'queued', 'retrying', 'delivered', 'failed')),
    next_attempt_at TEXT,
    final_failure_at TEXT,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audit_correlation_id TEXT NOT NULL CHECK (length(audit_correlation_id) = 36),
    deleted_at TEXT,
    CHECK (status != 'completed' OR completed_at IS NOT NULL),
    CHECK (status != 'cancelled' OR cancelled_at IS NOT NULL)
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    notes TEXT CHECK (notes IS NULL OR length(notes) <= 4000),
    due_date TEXT,
    due_time TEXT,
    timezone TEXT CHECK (timezone IS NULL OR length(timezone) BETWEEN 1 AND 64),
    priority TEXT NOT NULL CHECK (priority IN ('none', 'low', 'normal', 'high')),
    project_id TEXT REFERENCES projects(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'archived')),
    source TEXT NOT NULL CHECK (source IN ('alice', 'telegram', 'panel-agent', 'operator', 'ticktick', 'calendar')),
    source_ref TEXT CHECK (source_ref IS NULL OR length(source_ref) <= 256),
    completed_at TEXT,
    archived_at TEXT,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audit_correlation_id TEXT NOT NULL CHECK (length(audit_correlation_id) = 36),
    deleted_at TEXT,
    CHECK (due_time IS NULL OR (due_date IS NOT NULL AND timezone IS NOT NULL)),
    CHECK (status != 'completed' OR completed_at IS NOT NULL),
    CHECK (status != 'archived' OR archived_at IS NOT NULL)
);

CREATE TABLE calendar_events (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    notes TEXT CHECK (notes IS NULL OR length(notes) <= 4000),
    location TEXT CHECK (location IS NULL OR length(location) <= 1000),
    all_day INTEGER NOT NULL CHECK (all_day IN (0, 1)),
    start_at_utc TEXT,
    end_at_utc TEXT,
    start_date TEXT,
    end_date_exclusive TEXT,
    timezone TEXT NOT NULL CHECK (length(timezone) BETWEEN 1 AND 64),
    recurrence_rule TEXT CHECK (recurrence_rule IS NULL OR length(recurrence_rule) <= 2000),
    provider_id TEXT CHECK (provider_id IS NULL OR length(provider_id) <= 256),
    provider_calendar_id TEXT CHECK (provider_calendar_id IS NULL OR length(provider_calendar_id) <= 256),
    sync_state TEXT NOT NULL CHECK (sync_state IN ('local_only', 'pending', 'synced', 'stale', 'conflict', 'error')),
    source TEXT NOT NULL CHECK (source IN ('alice', 'telegram', 'panel-agent', 'operator', 'ticktick', 'calendar')),
    source_ref TEXT CHECK (source_ref IS NULL OR length(source_ref) <= 256),
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audit_correlation_id TEXT NOT NULL CHECK (length(audit_correlation_id) = 36),
    deleted_at TEXT,
    CHECK (
        (all_day = 0 AND start_at_utc IS NOT NULL AND end_at_utc IS NOT NULL
            AND start_date IS NULL AND end_date_exclusive IS NULL)
        OR
        (all_day = 1 AND start_at_utc IS NULL AND end_at_utc IS NULL
            AND start_date IS NOT NULL AND end_date_exclusive IS NOT NULL)
    )
);

CREATE TABLE idempotency_keys (
    audience TEXT NOT NULL CHECK (length(audience) BETWEEN 1 AND 64),
    key TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 256),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 71),
    response_json TEXT,
    response_status INTEGER CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    correlation_id TEXT,
    PRIMARY KEY (audience, key)
);

CREATE TABLE outbox (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    job_type TEXT NOT NULL CHECK (length(job_type) BETWEEN 1 AND 128),
    payload_version INTEGER NOT NULL CHECK (payload_version > 0),
    payload_json TEXT NOT NULL CHECK (length(payload_json) <= 1048576),
    status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled')),
    available_at TEXT NOT NULL,
    lease_owner TEXT CHECK (lease_owner IS NULL OR length(lease_owner) <= 128),
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 2000),
    correlation_id TEXT
);

CREATE TABLE delivery_attempts (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    reminder_id TEXT NOT NULL REFERENCES reminders(id) ON DELETE RESTRICT,
    channel TEXT NOT NULL CHECK (length(channel) BETWEEN 1 AND 64),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (status IN ('queued', 'started', 'succeeded', 'failed')),
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) <= 128),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 2000),
    provider_receipt TEXT CHECK (provider_receipt IS NULL OR length(provider_receipt) <= 512),
    created_at TEXT NOT NULL,
    UNIQUE (reminder_id, channel, attempt_number)
);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    actor_id TEXT,
    actor_type TEXT CHECK (actor_type IS NULL OR length(actor_type) <= 32),
    audience TEXT CHECK (audience IS NULL OR length(audience) <= 64),
    surface TEXT CHECK (surface IS NULL OR length(surface) <= 32),
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 128),
    object_domain TEXT NOT NULL CHECK (length(object_domain) BETWEEN 1 AND 64),
    object_id TEXT NOT NULL CHECK (length(object_id) = 36),
    old_version INTEGER CHECK (old_version IS NULL OR old_version > 0),
    new_version INTEGER CHECK (new_version IS NULL OR new_version > 0),
    correlation_id TEXT NOT NULL CHECK (length(correlation_id) = 36),
    before_json TEXT CHECK (before_json IS NULL OR length(before_json) <= 8192),
    after_json TEXT CHECK (after_json IS NULL OR length(after_json) <= 8192),
    created_at TEXT NOT NULL
);

CREATE TABLE provider_mappings (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    domain TEXT NOT NULL CHECK (domain IN ('task', 'calendar_event', 'project')),
    object_id TEXT NOT NULL CHECK (length(object_id) = 36),
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    external_id TEXT NOT NULL CHECK (length(external_id) BETWEEN 1 AND 512),
    external_calendar_id TEXT CHECK (external_calendar_id IS NULL OR length(external_calendar_id) <= 512),
    external_version TEXT CHECK (external_version IS NULL OR length(external_version) <= 256),
    external_etag TEXT CHECK (external_etag IS NULL OR length(external_etag) <= 512),
    last_exported_hash TEXT CHECK (last_exported_hash IS NULL OR length(last_exported_hash) <= 256),
    last_imported_hash TEXT CHECK (last_imported_hash IS NULL OR length(last_imported_hash) <= 256),
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sync_cursors (
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    scope TEXT NOT NULL CHECK (length(scope) BETWEEN 1 AND 512),
    cursor TEXT CHECK (cursor IS NULL OR length(cursor) <= 2000),
    last_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, scope)
);

CREATE TABLE sync_conflicts (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    domain TEXT NOT NULL CHECK (domain IN ('task', 'calendar_event', 'project')),
    object_id TEXT NOT NULL CHECK (length(object_id) = 36),
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    external_id TEXT NOT NULL CHECK (length(external_id) BETWEEN 1 AND 512),
    local_hash TEXT CHECK (local_hash IS NULL OR length(local_hash) <= 256),
    remote_hash TEXT CHECK (remote_hash IS NULL OR length(remote_hash) <= 256),
    details_json TEXT NOT NULL CHECK (length(details_json) <= 8192),
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'ignored')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_reminders_due
    ON reminders (status, due_at_utc)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_reminders_delivery
    ON reminders (delivery_state, next_attempt_at)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_tasks_due
    ON tasks (status, due_date, due_time)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_tasks_project_due
    ON tasks (project_id, status, due_date)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_calendar_timed_range
    ON calendar_events (start_at_utc, end_at_utc)
    WHERE deleted_at IS NULL AND all_day = 0;

CREATE INDEX idx_calendar_all_day_range
    ON calendar_events (start_date, end_date_exclusive)
    WHERE deleted_at IS NULL AND all_day = 1;

CREATE INDEX idx_idempotency_expiry
    ON idempotency_keys (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX idx_outbox_available
    ON outbox (status, available_at, lease_expires_at);

CREATE INDEX idx_delivery_attempts_reminder
    ON delivery_attempts (reminder_id, channel, attempt_number);

CREATE INDEX idx_audit_object
    ON audit_events (object_domain, object_id, created_at);

CREATE UNIQUE INDEX uq_provider_mapping_external
    ON provider_mappings (provider, external_id)
    WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX uq_provider_mapping_object
    ON provider_mappings (domain, object_id, provider)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_sync_conflicts_open
    ON sync_conflicts (status, provider, created_at);
