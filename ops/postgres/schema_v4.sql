ALTER TABLE v2_ai_packets
ADD COLUMN IF NOT EXISTS terminal_reason TEXT;

ALTER TABLE v2_ai_packets
ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ;

ALTER TABLE v2_ai_packets
DROP CONSTRAINT IF EXISTS v2_ai_packets_state_check;

ALTER TABLE v2_ai_packets
ADD CONSTRAINT v2_ai_packets_state_check
CHECK(state IN (
  'PENDING','CLAIMED','DECIDED','ERROR','EXPIRED','APPLIED','DEAD_LETTER'
));

CREATE INDEX IF NOT EXISTS v2_ai_packets_dead_letter_idx
ON v2_ai_packets(dead_lettered_at DESC)
WHERE state='DEAD_LETTER';
