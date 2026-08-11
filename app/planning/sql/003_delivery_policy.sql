-- A3 durable scheduler support.
--
-- A1/A2 already provide the outbox, leases, reminder delivery state and
-- per-channel attempts.  These additions are needed to make one reminder
-- delivery a durable, idempotently discoverable logical job and to prevent a
-- stale worker from committing after its lease has been reclaimed.

ALTER TABLE outbox ADD COLUMN dedupe_key TEXT;
ALTER TABLE outbox ADD COLUMN reminder_id TEXT;
ALTER TABLE outbox ADD COLUMN lease_token TEXT;
ALTER TABLE outbox ADD COLUMN attempt_window_started_at TEXT;
ALTER TABLE outbox ADD COLUMN last_error_code TEXT;
ALTER TABLE delivery_attempts ADD COLUMN correlation_id TEXT;

CREATE UNIQUE INDEX uq_outbox_dedupe_key
    ON outbox (dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE INDEX idx_outbox_reminder
    ON outbox (reminder_id, status, available_at);

CREATE INDEX idx_delivery_attempts_correlation
    ON delivery_attempts (correlation_id)
    WHERE correlation_id IS NOT NULL;
