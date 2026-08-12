CREATE OR REPLACE FUNCTION v2_record_service_heartbeat(
    p_service TEXT,
    p_status TEXT,
    p_details JSONB,
    p_recorded_at TIMESTAMPTZ
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    expected_service TEXT;
BEGIN
    expected_service := CASE session_user
        WHEN 'etoro-candidate' THEN 'v2-coordinator'
        WHEN 'etoro-ai' THEN 'v2-role-apply'
        WHEN 'etoro-decision' THEN 'v2-decision-shadow'
        WHEN 'etoro-decision-exec' THEN 'v2-decision-apply'
        WHEN 'etoro-exit' THEN 'v2-exit-manager'
        WHEN 'etoro-reconciler' THEN 'v2-reconciliation'
        WHEN 'etoro-executor' THEN 'v2-demo-executor'
        ELSE NULL
    END;
    IF expected_service IS NULL OR p_service IS DISTINCT FROM expected_service THEN
        RAISE insufficient_privilege USING MESSAGE = 'service heartbeat identity mismatch';
    END IF;
    IF p_status IS NULL OR btrim(p_status) = '' OR jsonb_typeof(p_details) <> 'object' THEN
        RAISE EXCEPTION 'service heartbeat payload is invalid';
    END IF;
    IF p_recorded_at IS NULL
       OR p_recorded_at > clock_timestamp() + interval '5 minutes'
       OR p_recorded_at < clock_timestamp() - interval '1 day' THEN
        RAISE EXCEPTION 'service heartbeat timestamp is invalid';
    END IF;

    INSERT INTO public.v2_service_heartbeats(service,status,details,recorded_at)
    VALUES(expected_service,p_status,p_details,p_recorded_at)
    ON CONFLICT(service) DO UPDATE SET
      status=EXCLUDED.status,
      details=EXCLUDED.details,
      recorded_at=EXCLUDED.recorded_at;
END;
$$;

REVOKE ALL ON FUNCTION v2_record_service_heartbeat(TEXT,TEXT,JSONB,TIMESTAMPTZ)
FROM PUBLIC;
