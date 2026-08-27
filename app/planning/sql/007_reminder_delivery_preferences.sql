-- Owner-controlled reminder delivery policy.  Destinations and credentials
-- remain server-side configuration; this row contains only fixed enums.
CREATE TABLE reminder_delivery_preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    spoken_endpoint TEXT NOT NULL CHECK (spoken_endpoint IN ('alice', 'jarvis')),
    phone_channels_json TEXT NOT NULL CHECK (length(phone_channels_json) <= 128),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_at TEXT NOT NULL
);
