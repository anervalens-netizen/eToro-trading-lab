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
    IF p_status NOT IN (
      'starting','synchronizing','healthy','resynchronizing','error','halted'
    ) THEN
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
