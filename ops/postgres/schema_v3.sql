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
