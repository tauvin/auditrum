CREATE OR REPLACE FUNCTION _audit_attach_context() RETURNS uuid AS $$
    DECLARE
        _ctx_id uuid;
        _ctx_metadata jsonb;
    BEGIN
        BEGIN
            _ctx_id := current_setting('{guc_id}')::uuid;
            _ctx_metadata := current_setting('{guc_metadata}')::jsonb;
            EXCEPTION WHEN OTHERS THEN
                _ctx_id := NULL;
                _ctx_metadata := NULL;
        END;
        IF _ctx_id IS NOT NULL AND _ctx_metadata IS NOT NULL THEN
            INSERT INTO {context_table} (id, metadata, created_at, updated_at)
                VALUES (_ctx_id, _ctx_metadata, now(), now())
                ON CONFLICT (id) DO UPDATE
                    SET metadata = EXCLUDED.metadata,
                        updated_at = EXCLUDED.updated_at
                    WHERE {context_table}.metadata != EXCLUDED.metadata;
            RETURN _ctx_id;
        ELSE
            RETURN NULL;
        END IF;
    END;
$$ LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public;
