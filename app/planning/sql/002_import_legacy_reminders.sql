-- A2 structural support for the runtime legacy reminder import.
-- This migration stores metadata only; it never embeds source reminder data.

CREATE TABLE legacy_reminder_imports (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    import_version TEXT NOT NULL CHECK (length(import_version) BETWEEN 1 AND 64),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 71),
    status TEXT NOT NULL CHECK (status IN ('completed')),
    imported_count INTEGER NOT NULL CHECK (imported_count >= 0),
    mapping_count INTEGER NOT NULL CHECK (mapping_count >= 0),
    semantic_hash TEXT NOT NULL CHECK (length(semantic_hash) = 71),
    report_json TEXT NOT NULL CHECK (length(report_json) <= 32768),
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    UNIQUE (import_version)
);

CREATE TABLE legacy_reminder_mappings (
    planning_id TEXT PRIMARY KEY REFERENCES reminders(id) ON DELETE RESTRICT,
    origin TEXT NOT NULL CHECK (origin IN ('legacy', 'native')),
    legacy_id TEXT UNIQUE CHECK (legacy_id IS NULL OR length(legacy_id) = 12),
    legacy_source TEXT CHECK (legacy_source IS NULL OR legacy_source IN ('alice', 'telegram')),
    legacy_status TEXT CHECK (legacy_status IS NULL OR legacy_status IN ('pending', 'fired', 'cancelled')),
    legacy_created_at TEXT CHECK (legacy_created_at IS NULL OR length(legacy_created_at) <= 128),
    legacy_created_at_utc TEXT CHECK (legacy_created_at_utc IS NULL OR length(legacy_created_at_utc) <= 64),
    legacy_due_at TEXT CHECK (legacy_due_at IS NULL OR length(legacy_due_at) <= 128),
    legacy_due_at_utc TEXT CHECK (legacy_due_at_utc IS NULL OR length(legacy_due_at_utc) <= 64),
    legacy_fired_at TEXT CHECK (legacy_fired_at IS NULL OR length(legacy_fired_at) <= 128),
    legacy_fired_at_utc TEXT CHECK (legacy_fired_at_utc IS NULL OR length(legacy_fired_at_utc) <= 64),
    legacy_cancelled_at TEXT CHECK (legacy_cancelled_at IS NULL OR length(legacy_cancelled_at) <= 128),
    legacy_cancelled_at_utc TEXT CHECK (legacy_cancelled_at_utc IS NULL OR length(legacy_cancelled_at_utc) <= 64),
    legacy_chat_id INTEGER,
    legacy_delay_seconds INTEGER CHECK (legacy_delay_seconds IS NULL OR legacy_delay_seconds >= 0),
    import_version TEXT CHECK (import_version IS NULL OR length(import_version) BETWEEN 1 AND 64),
    source_sha256 TEXT CHECK (source_sha256 IS NULL OR length(source_sha256) = 71),
    inferred_semantics TEXT CHECK (inferred_semantics IS NULL OR inferred_semantics = 'legacy_delivery_inferred'),
    created_at TEXT NOT NULL,
    CHECK (
        (origin = 'legacy'
            AND legacy_id IS NOT NULL
            AND legacy_source IS NOT NULL
            AND legacy_status IS NOT NULL
            AND import_version IS NOT NULL
            AND source_sha256 IS NOT NULL)
        OR
        (origin = 'native'
            AND legacy_id IS NULL
            AND legacy_source IS NULL
            AND legacy_status IS NULL
            AND import_version IS NULL
            AND source_sha256 IS NULL
            AND inferred_semantics IS NULL)
    )
);

CREATE INDEX idx_legacy_reminder_mappings_import
    ON legacy_reminder_mappings (import_version, legacy_id)
    WHERE origin = 'legacy';
