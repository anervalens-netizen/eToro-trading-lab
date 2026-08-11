UPDATE v2_ai_packets
SET state='DEAD_LETTER',claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL,
    terminal_reason=coalesce(terminal_reason,'invalid_inference_lease_migrated'),
    dead_lettered_at=coalesce(dead_lettered_at,now()),updated_at=now()
WHERE state='CLAIMED'
  AND (claimed_by IS NULL OR claim_token IS NULL OR lease_expires_at IS NULL);

UPDATE v2_ai_packets
SET claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL
WHERE state<>'CLAIMED';

UPDATE v2_ai_packets
SET apply_claimed_by=NULL,apply_claim_token=NULL,apply_lease_expires_at=NULL
WHERE state<>'DECIDED'
   OR (apply_claimed_by IS NULL OR apply_claim_token IS NULL OR apply_lease_expires_at IS NULL);

ALTER TABLE v2_ai_packets
DROP CONSTRAINT IF EXISTS v2_ai_packets_inference_lease_check;
ALTER TABLE v2_ai_packets
ADD CONSTRAINT v2_ai_packets_inference_lease_check CHECK(
  (state='CLAIMED' AND claimed_by IS NOT NULL AND claim_token IS NOT NULL
                   AND lease_expires_at IS NOT NULL)
  OR
  (state<>'CLAIMED' AND claimed_by IS NULL AND claim_token IS NULL
                    AND lease_expires_at IS NULL)
);

ALTER TABLE v2_ai_packets
DROP CONSTRAINT IF EXISTS v2_ai_packets_apply_lease_check;
ALTER TABLE v2_ai_packets
ADD CONSTRAINT v2_ai_packets_apply_lease_check CHECK(
  (apply_claimed_by IS NULL AND apply_claim_token IS NULL AND apply_lease_expires_at IS NULL)
  OR
  (state='DECIDED' AND apply_claimed_by IS NOT NULL AND apply_claim_token IS NOT NULL
                   AND apply_lease_expires_at IS NOT NULL)
);

CREATE OR REPLACE FUNCTION v2_record_market_heartbeat(
    p_status TEXT,
    p_details JSONB,
    p_recorded_at TIMESTAMPTZ
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF p_status NOT IN ('starting','healthy','resynchronizing','error','halted') THEN
        RAISE EXCEPTION 'invalid market heartbeat status';
    END IF;
    IF jsonb_typeof(p_details) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid market heartbeat details';
    END IF;
    IF p_recorded_at < now() - interval '5 minutes'
       OR p_recorded_at > now() + interval '30 seconds' THEN
        RAISE EXCEPTION 'invalid market heartbeat time';
    END IF;
    INSERT INTO public.v2_service_heartbeats(service,status,details,recorded_at)
    VALUES('v2-market',p_status,p_details,p_recorded_at)
    ON CONFLICT(service) DO UPDATE
    SET status=EXCLUDED.status,details=EXCLUDED.details,recorded_at=EXCLUDED.recorded_at;
END;
$$;

REVOKE ALL ON FUNCTION v2_record_market_heartbeat(TEXT,JSONB,TIMESTAMPTZ) FROM PUBLIC;
