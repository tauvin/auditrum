CREATE OR REPLACE FUNCTION _audit_filter_row(p_row jsonb, p_spec jsonb)
RETURNS jsonb AS $$
    SELECT CASE p_spec->>'fields_kind'
        WHEN 'exclude' THEN
            p_row - ARRAY(SELECT jsonb_array_elements_text(p_spec->'fields'))
        WHEN 'only' THEN
            COALESCE((
                SELECT jsonb_object_agg(r.key, r.value)
                FROM jsonb_each(p_row) AS r(key, value)
                WHERE r.key IN (SELECT jsonb_array_elements_text(p_spec->'fields'))
            ), '{{}}'::jsonb)
        ELSE p_row
    END;
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION _audit_tracked_spec(p_table text)
RETURNS jsonb AS $$
DECLARE
    v_spec jsonb;
BEGIN
    -- The registry is created lazily by TriggerManager.bootstrap(), so it
    -- may not exist yet; PL/pgSQL resolves the name at call time.
    IF to_regclass('{tracking_table}') IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT r.spec_fingerprint INTO v_spec
    FROM {tracking_table} r
    WHERE r.table_name = p_table
      AND r.spec_fingerprint->>'audit_table' = '{audit_table}'
    ORDER BY r.trigger_name
    LIMIT 1;
    RETURN v_spec;
END;
$$ LANGUAGE plpgsql STABLE SECURITY INVOKER;

CREATE OR REPLACE FUNCTION _audit_live_row(p_table text, p_object_id text, p_spec jsonb)
RETURNS jsonb AS $$
DECLARE
    v_pk_type text;
    v_row jsonb;
BEGIN
    -- Cast the text object id to the real PK type so the PK index is used.
    SELECT format_type(a.atttypid, a.atttypmod) INTO v_pk_type
    FROM pg_attribute a
    WHERE a.attrelid = quote_ident(p_table)::regclass
      AND a.attname = 'id'
      AND NOT a.attisdropped;
    EXECUTE format('SELECT to_jsonb(t) FROM %I t WHERE t.id = $1::%s', p_table, v_pk_type)
        INTO v_row USING p_object_id;
    IF v_row IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN _audit_filter_row(v_row, p_spec);
END;
$$ LANGUAGE plpgsql STABLE SECURITY INVOKER;

CREATE OR REPLACE FUNCTION _audit_live_rows(p_table text, p_spec jsonb)
RETURNS TABLE(object_id text, row_data jsonb) AS $$
BEGIN
    -- The caller passes NULL when the live table is not needed.
    IF p_spec IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY EXECUTE format(
        'SELECT to_jsonb(t)->>''id'', _audit_filter_row(to_jsonb(t), $1) FROM %I t',
        p_table
    ) USING p_spec;
END;
$$ LANGUAGE plpgsql STABLE SECURITY INVOKER;
