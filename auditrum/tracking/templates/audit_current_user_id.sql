CREATE OR REPLACE FUNCTION _audit_current_user_id() RETURNS integer AS $$
    DECLARE
        _meta jsonb;
    BEGIN
        BEGIN
            _meta := current_setting('{guc_metadata}')::jsonb;
            RETURN (_meta->>'user_id')::integer;
            EXCEPTION WHEN OTHERS THEN
                RETURN NULL;
        END;
    END;
$$ LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public;
