CREATE OR REPLACE FUNCTION v2_lock_ai_authority()
RETURNS TABLE(state TEXT,version BIGINT)
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
    IF NOT COALESCE(invoker_super, FALSE)
       AND invoker NOT LIKE 'etoro-ai%'
       AND invoker NOT LIKE 'etoro-candidate%'
       AND invoker NOT LIKE 'etoro-decision%' THEN
        RAISE EXCEPTION 'role may not lock AI authority';
    END IF;
    RETURN QUERY
    SELECT current_state.state,current_state.version
    FROM public.v2_trading_state current_state
    WHERE singleton=TRUE
    FOR SHARE;
END;
$$;

REVOKE ALL ON FUNCTION v2_lock_ai_authority() FROM PUBLIC;

CREATE OR REPLACE FUNCTION v2_enforce_economic_event_authority()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    invoker TEXT;
    invoker_super BOOLEAN := FALSE;
    active_schema_version INTEGER;
    legacy_engine_compatible BOOLEAN := FALSE;
    projected_position_id TEXT;
    projected_status TEXT;
BEGIN
    IF NEW.event_type NOT IN ('PositionClosed','PositionReduced') THEN
        RETURN NEW;
    END IF;
    invoker := current_setting('role', TRUE);
    IF invoker IS NULL OR invoker = '' OR invoker = 'none' THEN
        invoker := session_user;
    END IF;
    SELECT rolsuper INTO invoker_super FROM pg_catalog.pg_roles WHERE rolname=invoker;
    SELECT CASE
        WHEN value ~ '^[1-9][0-9]*$' THEN value::INTEGER
        ELSE NULL
    END
    INTO active_schema_version
    FROM public.v2_meta
    WHERE key='schema_version';

    -- Failed release cutover restores the previous schema marker before the
    -- old units are restarted. Migrations are append-only, so the v8 trigger
    -- remains installed. Permit only the fully privileged legacy engine to
    -- finish its old atomic fill/position/event projection while that older
    -- marker is active. Current service roles cannot satisfy this capability
    -- set, and restoring marker 8 immediately re-enables strict v8 authority.
    legacy_engine_compatible := active_schema_version IS NOT NULL
        AND active_schema_version < 8
        AND invoker LIKE 'etoro-engine%'
        AND has_table_privilege(invoker,'public.v2_events','INSERT')
        AND has_table_privilege(invoker,'public.v2_fills','INSERT')
        AND has_table_privilege(invoker,'public.v2_positions','INSERT')
        AND has_table_privilege(invoker,'public.v2_positions','UPDATE')
        AND has_table_privilege(invoker,'public.v2_broker_orders','UPDATE')
        AND has_table_privilege(invoker,'public.v2_risk_reservations','UPDATE');
    IF legacy_engine_compatible THEN
        RETURN NEW;
    END IF;
    IF NOT COALESCE(invoker_super, FALSE) AND invoker NOT LIKE 'etoro-reconciler%' THEN
        RAISE EXCEPTION 'economic position events require reconciler authority';
    END IF;
    IF jsonb_typeof(NEW.payload) <> 'object'
       OR jsonb_typeof(NEW.payload->'position') <> 'object'
       OR jsonb_typeof(NEW.payload->'realized_delta_usd') <> 'string'
       OR NEW.causation_id IS NULL
       OR btrim(NEW.causation_id) = ''
       OR NOT EXISTS(
           SELECT 1 FROM public.v2_fills fill
           WHERE fill.fill_id=NEW.causation_id
       ) THEN
        RAISE EXCEPTION 'economic position event lacks canonical fill provenance';
    END IF;
    projected_position_id := NEW.payload->'position'->>'position_id';
    projected_status := CASE NEW.event_type
        WHEN 'PositionClosed' THEN 'CLOSED'
        ELSE 'OPEN'
    END;
    IF projected_position_id IS NULL OR btrim(projected_position_id) = ''
       OR NOT EXISTS(
           SELECT 1 FROM public.v2_positions position
           WHERE position.position_id=projected_position_id
             AND position.status=projected_status
             AND position.state=NEW.payload->'position'
       ) THEN
        RAISE EXCEPTION 'economic position event lacks canonical position projection';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION v2_enforce_economic_event_authority() FROM PUBLIC;

DROP TRIGGER IF EXISTS v2_events_economic_authority ON v2_events;
CREATE TRIGGER v2_events_economic_authority
BEFORE INSERT ON v2_events
FOR EACH ROW EXECUTE FUNCTION v2_enforce_economic_event_authority();

CREATE OR REPLACE FUNCTION v2_append_ai_telemetry_event(
    p_event_id TEXT,
    p_event_type TEXT,
    p_schema_version INTEGER,
    p_event_time TIMESTAMPTZ,
    p_processing_time TIMESTAMPTZ,
    p_idempotency_key TEXT,
    p_causation_id TEXT,
    p_correlation_id TEXT,
    p_payload JSONB,
    p_canonical_body TEXT
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    invoker TEXT;
    invoker_super BOOLEAN := FALSE;
    packet_identity TEXT;
    expected_key TEXT;
    expected_event_id TEXT;
    body JSONB;
    body_hash TEXT;
    previous TEXT;
    digest TEXT;
    existing RECORD;
BEGIN
    invoker := current_setting('role', TRUE);
    IF invoker IS NULL OR invoker = '' OR invoker = 'none' THEN
        invoker := session_user;
    END IF;
    SELECT rolsuper INTO invoker_super FROM pg_catalog.pg_roles WHERE rolname=invoker;
    IF NOT COALESCE(invoker_super, FALSE)
       AND invoker NOT LIKE 'etoro-ai%'
       AND invoker NOT LIKE 'etoro-decision%' THEN
        RAISE EXCEPTION 'role may not append AI telemetry';
    END IF;

    IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object'
       OR jsonb_typeof(p_payload->'actor') <> 'string'
       OR p_payload->>'actor' IS DISTINCT FROM invoker
       OR jsonb_typeof(p_payload->'packet_id') <> 'string'
       OR p_payload->>'packet_id' !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$' THEN
        RAISE EXCEPTION 'AI telemetry payload identity is invalid';
    END IF;
    packet_identity := p_payload->>'packet_id';

    IF p_event_type = 'AIPacketDeadLettered' THEN
        IF p_schema_version <> 4
           OR (SELECT count(*) FROM jsonb_object_keys(p_payload)) <> 5
           OR NOT p_payload ?& ARRAY['actor','packet_id','stage','reason','attempt']
           OR jsonb_typeof(p_payload->'stage') <> 'string'
           OR p_payload->>'stage' NOT IN ('inference','apply')
           OR jsonb_typeof(p_payload->'reason') <> 'string'
           OR btrim(p_payload->>'reason') = ''
           OR length(p_payload->>'reason') > 200
           OR jsonb_typeof(p_payload->'attempt') <> 'number'
           OR p_payload->>'attempt' !~ '^[1-9][0-9]{0,2}$' THEN
            RAISE EXCEPTION 'AI dead-letter telemetry payload is invalid';
        END IF;
        expected_key := format(
            'ai-dead-letter:%s:%s:%s',
            packet_identity,
            p_payload->>'stage',
            p_payload->>'attempt'
        );
        IF NOT EXISTS(
            SELECT 1 FROM public.v2_ai_packets packet
            WHERE packet.packet_id=packet_identity
              AND packet.state='DEAD_LETTER'
              AND packet.terminal_reason=p_payload->>'reason'
              AND CASE p_payload->>'stage'
                  WHEN 'inference' THEN packet.attempt_count
                  ELSE packet.apply_attempt_count
              END=(p_payload->>'attempt')::INTEGER
        ) THEN
            RAISE EXCEPTION 'AI dead-letter telemetry lacks canonical queue provenance';
        END IF;
    ELSIF p_event_type = 'AIPacketAuthorityExpired' THEN
        IF p_schema_version <> 6
           OR (SELECT count(*) FROM jsonb_object_keys(p_payload)) <> 5
           OR NOT p_payload ?& ARRAY[
               'actor','packet_id','required_authority_mode',
               'required_execution_epoch','broker_write'
           ]
           OR jsonb_typeof(p_payload->'required_authority_mode') <> 'string'
           OR p_payload->>'required_authority_mode' NOT IN ('SHADOW','EXECUTION')
           OR p_payload->'broker_write' <> 'false'::jsonb
           OR (
               p_payload->>'required_authority_mode' = 'SHADOW'
               AND jsonb_typeof(p_payload->'required_execution_epoch') <> 'null'
           )
           OR (
               p_payload->>'required_authority_mode' = 'EXECUTION'
               AND (
                   jsonb_typeof(p_payload->'required_execution_epoch') <> 'number'
                   OR p_payload->>'required_execution_epoch' !~ '^[1-9][0-9]*$'
               )
           ) THEN
            RAISE EXCEPTION 'AI authority telemetry payload is invalid';
        END IF;
        expected_key := format(
            'ai-authority-expired:%s:%s:%s',
            packet_identity,
            p_payload->>'required_authority_mode',
            CASE
                WHEN jsonb_typeof(p_payload->'required_execution_epoch') = 'null'
                THEN 'None'
                ELSE p_payload->>'required_execution_epoch'
            END
        );
        IF NOT EXISTS(
            SELECT 1 FROM public.v2_ai_packets packet
            WHERE packet.packet_id=packet_identity
              AND packet.state='EXPIRED'
              AND packet.terminal_reason='authority_epoch_closed'
        ) THEN
            RAISE EXCEPTION 'AI authority telemetry lacks canonical queue provenance';
        END IF;
    ELSE
        RAISE EXCEPTION 'AI telemetry event type is not allowed';
    END IF;

    expected_event_id := 'evt-' || substr(
        encode(sha256(convert_to(expected_key, 'UTF8')), 'hex'),
        1,
        24
    );
    IF p_idempotency_key IS DISTINCT FROM expected_key
       OR p_event_id IS DISTINCT FROM expected_event_id
       OR p_causation_id IS DISTINCT FROM packet_identity
       OR p_correlation_id IS DISTINCT FROM packet_identity
       OR p_event_time IS NULL
       OR p_processing_time IS DISTINCT FROM p_event_time
       OR p_processing_time < now() - interval '1 day'
       OR p_processing_time > now() + interval '30 seconds' THEN
        RAISE EXCEPTION 'AI telemetry envelope is invalid';
    END IF;

    BEGIN
        body := p_canonical_body::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'AI telemetry canonical body is invalid';
    END;
    IF jsonb_typeof(body) <> 'object'
       OR (SELECT count(*) FROM jsonb_object_keys(body)) <> 9
       OR NOT body ?& ARRAY[
           'event_id','event_type','schema_version','event_time','processing_time',
           'idempotency_key','causation_id','correlation_id','payload'
       ]
       OR body->>'event_id' IS DISTINCT FROM p_event_id
       OR body->>'event_type' IS DISTINCT FROM p_event_type
       OR body->>'schema_version' IS DISTINCT FROM p_schema_version::TEXT
       OR (body->>'event_time')::TIMESTAMPTZ IS DISTINCT FROM p_event_time
       OR (body->>'processing_time')::TIMESTAMPTZ IS DISTINCT FROM p_processing_time
       OR body->>'idempotency_key' IS DISTINCT FROM p_idempotency_key
       OR body->>'causation_id' IS DISTINCT FROM p_causation_id
       OR body->>'correlation_id' IS DISTINCT FROM p_correlation_id
       OR body->'payload' IS DISTINCT FROM p_payload THEN
        RAISE EXCEPTION 'AI telemetry canonical body disagrees with envelope';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('etoro_v2_event_chain'));
    body_hash := encode(sha256(convert_to(p_canonical_body, 'UTF8')), 'hex');
    SELECT event_hash,canonical_body,canonical_body_hash,event_id
    INTO existing
    FROM public.v2_events
    WHERE idempotency_key=p_idempotency_key OR event_id=p_event_id
    ORDER BY CASE WHEN idempotency_key=p_idempotency_key THEN 0 ELSE 1 END
    LIMIT 1;
    IF FOUND THEN
        IF existing.event_id IS DISTINCT FROM p_event_id
           OR existing.canonical_body IS DISTINCT FROM p_canonical_body
           OR btrim(existing.canonical_body_hash) IS DISTINCT FROM body_hash THEN
            RAISE EXCEPTION 'AI telemetry idempotency conflict';
        END IF;
        RETURN btrim(existing.event_hash);
    END IF;

    SELECT btrim(event_hash) INTO previous
    FROM public.v2_events ORDER BY sequence DESC LIMIT 1;
    previous := COALESCE(previous, repeat('0', 64));
    digest := encode(sha256(convert_to(previous || p_canonical_body, 'UTF8')), 'hex');
    INSERT INTO public.v2_events(
        event_id,event_type,schema_version,event_time,processing_time,idempotency_key,
        causation_id,correlation_id,payload,canonical_body,canonical_body_hash,
        previous_hash,event_hash
    ) VALUES(
        p_event_id,p_event_type,p_schema_version,p_event_time,p_processing_time,
        p_idempotency_key,p_causation_id,p_correlation_id,p_payload,p_canonical_body,
        body_hash,previous,digest
    );
    RETURN digest;
END;
$$;

REVOKE ALL ON FUNCTION v2_append_ai_telemetry_event(
    TEXT,TEXT,INTEGER,TIMESTAMPTZ,TIMESTAMPTZ,TEXT,TEXT,TEXT,JSONB,TEXT
) FROM PUBLIC;
