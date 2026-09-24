-- Internal provider write metadata. Never project these fields into Planning API envelopes.
ALTER TABLE provider_calendars ADD COLUMN collection_ref TEXT
    CHECK (collection_ref IS NULL OR length(collection_ref) BETWEEN 1 AND 512);
ALTER TABLE provider_calendars ADD COLUMN can_read INTEGER CHECK (can_read IS NULL OR can_read IN (0, 1));
ALTER TABLE provider_calendars ADD COLUMN can_write INTEGER CHECK (can_write IS NULL OR can_write IN (0, 1));
ALTER TABLE provider_event_cache ADD COLUMN provider_etag TEXT
    CHECK (provider_etag IS NULL OR length(provider_etag) BETWEEN 1 AND 220);
ALTER TABLE provider_event_cache ADD COLUMN write_safe INTEGER NOT NULL DEFAULT 0
    CHECK (write_safe IN (0, 1));
