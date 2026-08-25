-- Persist successful deletion evidence separately from generic provider staleness.

ALTER TABLE provider_event_cache
    ADD COLUMN missing_successes INTEGER NOT NULL DEFAULT 0
    CHECK (missing_successes BETWEEN 0 AND 1);
