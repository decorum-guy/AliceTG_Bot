-- A6 persistent, short-lived Telegram mutation capabilities.
--
-- The callback only carries a high-entropy opaque token.  The digest binds it
-- to one closed action, one canonical object/version and one Telegram
-- principal.  Domain IDs are intentionally not foreign keys because the
-- action table is polymorphic across the frozen Planning domains.

CREATE TABLE telegram_action_tokens (
    token_digest TEXT PRIMARY KEY CHECK (length(token_digest) = 64),
    action TEXT NOT NULL CHECK (
        action IN ('reminder_complete', 'reminder_cancel', 'reminder_retry', 'task_complete')
    ),
    domain TEXT NOT NULL CHECK (domain IN ('reminder', 'task')),
    object_id TEXT NOT NULL CHECK (length(object_id) = 36),
    expected_version INTEGER NOT NULL CHECK (expected_version > 0),
    telegram_user_id INTEGER NOT NULL,
    telegram_chat_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX idx_telegram_action_tokens_expiry
    ON telegram_action_tokens (expires_at);

CREATE INDEX idx_telegram_action_tokens_binding
    ON telegram_action_tokens (telegram_user_id, telegram_chat_id, expires_at);
