ALTER TABLE v2_ai_packets
ADD COLUMN IF NOT EXISTS authority_mode TEXT NOT NULL DEFAULT 'SHADOW';

ALTER TABLE v2_ai_packets
ADD COLUMN IF NOT EXISTS execution_epoch BIGINT;

-- Every packet that predates authority binding is shadow evidence only. It may
-- still be inspected, but it can never cross into an execution epoch.
UPDATE v2_ai_packets
SET authority_mode='SHADOW',execution_epoch=NULL
WHERE authority_mode IS DISTINCT FROM 'SHADOW' AND execution_epoch IS NULL;

ALTER TABLE v2_ai_packets
DROP CONSTRAINT IF EXISTS v2_ai_packets_authority_check;

ALTER TABLE v2_ai_packets
ADD CONSTRAINT v2_ai_packets_authority_check CHECK(
  (authority_mode='SHADOW' AND execution_epoch IS NULL)
  OR
  (authority_mode='EXECUTION' AND execution_epoch IS NOT NULL AND execution_epoch>=1)
);

CREATE INDEX IF NOT EXISTS v2_ai_packets_authority_claim_idx
ON v2_ai_packets(role,state,authority_mode,execution_epoch,updated_at,packet_id);
