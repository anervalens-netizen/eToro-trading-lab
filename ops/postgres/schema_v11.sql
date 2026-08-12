DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etoro-control') THEN
        GRANT SELECT ON TABLE public.v2_meta, public.v2_schema_migrations
        TO "etoro-control";
    END IF;
END
$$;
