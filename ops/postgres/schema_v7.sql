CREATE OR REPLACE FUNCTION v2_trip_audit_integrity_failure()
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    UPDATE public.v2_trading_state
    SET state='LOCKED',
        actor='audit-integrity-guard',
        reason='event idempotency conflict',
        version=version+1,
        changed_at=now()
    WHERE singleton=TRUE;

    INSERT INTO public.v2_meta(key,value,updated_at)
    VALUES('audit_integrity_failure','event_idempotency_conflict',now())
    ON CONFLICT(key) DO UPDATE
    SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at;
END;
$$;

REVOKE ALL ON FUNCTION v2_trip_audit_integrity_failure() FROM PUBLIC;

CREATE OR REPLACE FUNCTION v2_update_peak_equity(incoming NUMERIC)
RETURNS NUMERIC
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    peak NUMERIC;
BEGIN
    IF incoming IS NULL OR incoming <= 0 OR incoming = 'NaN'::NUMERIC THEN
        RAISE EXCEPTION 'peak equity candidate must be finite and positive';
    END IF;
    INSERT INTO public.v2_meta(key,value,updated_at)
    VALUES('broker_peak_equity_v2',incoming::TEXT,now())
    ON CONFLICT(key) DO UPDATE
    SET value=GREATEST(public.v2_meta.value::NUMERIC,EXCLUDED.value::NUMERIC)::TEXT,
        updated_at=EXCLUDED.updated_at
    RETURNING value::NUMERIC INTO peak;
    RETURN peak;
END;
$$;

REVOKE ALL ON FUNCTION v2_update_peak_equity(NUMERIC) FROM PUBLIC;

CREATE OR REPLACE FUNCTION v2_set_runtime_meta(
    p_key TEXT,
    p_value TEXT,
    p_updated_at TIMESTAMPTZ
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    invoker TEXT;
    invoker_super BOOLEAN := FALSE;
BEGIN
    invoker := current_setting('role', TRUE);
    IF invoker IS NULL OR invoker = '' OR invoker = 'none' THEN
        invoker := session_user;
    END IF;
    SELECT rolsuper INTO invoker_super FROM pg_catalog.pg_roles WHERE rolname=invoker;
    IF p_key IS NULL OR p_key !~ '^[a-z0-9][a-z0-9_.:-]{0,199}$'
       OR p_value IS NULL OR length(p_value) > 10000
       OR p_updated_at IS NULL
       OR p_updated_at < now() - interval '1 day'
       OR p_updated_at > now() + interval '30 seconds' THEN
        RAISE EXCEPTION 'runtime metadata is invalid';
    END IF;
    IF p_key IN ('schema_version','broker_peak_equity_v2','audit_integrity_failure') THEN
        RAISE EXCEPTION 'runtime metadata key is protected';
    END IF;
    IF NOT invoker_super THEN
        IF invoker LIKE 'etoro-candidate%' AND p_key !~ '^last_coordinated_bar:' THEN
            RAISE EXCEPTION 'candidate metadata namespace is restricted';
        ELSIF invoker LIKE 'etoro-ai%' AND p_key !~ '^latest_(regime|critic)_v2:' THEN
            RAISE EXCEPTION 'AI metadata namespace is restricted';
        ELSIF invoker LIKE 'etoro-reconciler%'
              AND p_key <> 'v2_reconciliation_history_evidence' THEN
            RAISE EXCEPTION 'reconciler metadata namespace is restricted';
        ELSIF invoker NOT LIKE 'etoro-candidate%'
              AND invoker NOT LIKE 'etoro-ai%'
              AND invoker NOT LIKE 'etoro-reconciler%' THEN
            RAISE EXCEPTION 'runtime role has no metadata namespace';
        END IF;
    END IF;
    INSERT INTO public.v2_meta(key,value,updated_at)
    VALUES(p_key,p_value,p_updated_at)
    ON CONFLICT(key) DO UPDATE
    SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at;
END;
$$;

REVOKE ALL ON FUNCTION v2_set_runtime_meta(TEXT,TEXT,TIMESTAMPTZ) FROM PUBLIC;

CREATE OR REPLACE FUNCTION v2_transition_trading_state(
    p_state TEXT,
    p_actor TEXT,
    p_reason TEXT,
    p_changed_at TIMESTAMPTZ
)
RETURNS TABLE(previous_state TEXT,new_version BIGINT,changed BOOLEAN)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    invoker TEXT;
    invoker_super BOOLEAN := FALSE;
    prior_rank INTEGER;
    target_rank INTEGER;
BEGIN
    invoker := current_setting('role', TRUE);
    IF invoker IS NULL OR invoker = '' OR invoker = 'none' THEN
        invoker := session_user;
    END IF;
    SELECT rolsuper INTO invoker_super FROM pg_catalog.pg_roles WHERE rolname=invoker;
    IF p_state NOT IN ('ACTIVE','HALT_NEW','REDUCE_ONLY','LOCKED')
       OR p_actor IS NULL OR btrim(p_actor) = '' OR length(p_actor) > 200
       OR p_reason IS NULL OR btrim(p_reason) = '' OR length(p_reason) > 500
       OR p_changed_at IS NULL
       OR p_changed_at < now() - interval '5 minutes'
       OR p_changed_at > now() + interval '30 seconds' THEN
        RAISE EXCEPTION 'trading state transition is invalid';
    END IF;

    SELECT state,version INTO previous_state,new_version
    FROM public.v2_trading_state WHERE singleton=TRUE FOR UPDATE;
    IF previous_state IS NULL THEN
        RAISE EXCEPTION 'trading state singleton is missing';
    END IF;
    changed := previous_state IS DISTINCT FROM p_state;
    IF NOT changed THEN
        RETURN NEXT;
        RETURN;
    END IF;

    prior_rank := CASE previous_state
        WHEN 'ACTIVE' THEN 0 WHEN 'HALT_NEW' THEN 1
        WHEN 'REDUCE_ONLY' THEN 2 WHEN 'LOCKED' THEN 3 END;
    target_rank := CASE p_state
        WHEN 'ACTIVE' THEN 0 WHEN 'HALT_NEW' THEN 1
        WHEN 'REDUCE_ONLY' THEN 2 WHEN 'LOCKED' THEN 3 END;
    IF NOT invoker_super AND invoker NOT LIKE 'etoro-control%' AND target_rank < prior_rank THEN
        RAISE EXCEPTION 'runtime role may only restrict trading state';
    END IF;
    IF p_state = 'ACTIVE' AND NOT invoker_super AND invoker NOT LIKE 'etoro-control%' THEN
        RAISE EXCEPTION 'only control authority may activate trading';
    END IF;

    new_version := new_version + 1;
    UPDATE public.v2_trading_state
    SET state=p_state,actor=p_actor,reason=p_reason,version=new_version,changed_at=p_changed_at
    WHERE singleton=TRUE;
    RETURN NEXT;
END;
$$;

REVOKE ALL ON FUNCTION v2_transition_trading_state(TEXT,TEXT,TEXT,TIMESTAMPTZ) FROM PUBLIC;

CREATE OR REPLACE FUNCTION v2_lock_trading_state()
RETURNS TABLE(state TEXT,version BIGINT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT state,version
    FROM public.v2_trading_state
    WHERE singleton=TRUE
    FOR UPDATE;
$$;

REVOKE ALL ON FUNCTION v2_lock_trading_state() FROM PUBLIC;
