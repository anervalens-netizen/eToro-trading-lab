-- The append-only trigger protects runtime writes, but this migration must
-- populate a derived integrity column for pre-v3 events. Dropping and
-- recreating the trigger occurs inside the migration transaction, so any
-- failure restores the original trigger and event rows atomically.
DROP TRIGGER IF EXISTS v2_events_append_only ON v2_events;

ALTER TABLE v2_events
ADD COLUMN IF NOT EXISTS canonical_body_hash CHAR(64);

UPDATE v2_events
SET canonical_body_hash = encode(sha256(convert_to(canonical_body, 'UTF8')), 'hex')
WHERE canonical_body_hash IS NULL;

ALTER TABLE v2_events
ALTER COLUMN canonical_body_hash SET NOT NULL;

ALTER TABLE v2_events
DROP CONSTRAINT IF EXISTS v2_events_canonical_body_hash_check;

ALTER TABLE v2_events
ADD CONSTRAINT v2_events_canonical_body_hash_check
CHECK(canonical_body_hash ~ '^[0-9a-f]{64}$');

CREATE TRIGGER v2_events_append_only BEFORE UPDATE OR DELETE ON v2_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_append_only_mutation();
