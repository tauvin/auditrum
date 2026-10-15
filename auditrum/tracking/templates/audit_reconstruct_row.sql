CREATE OR REPLACE FUNCTION _audit_reconstruct_row(
    p_table text,
    p_object_id text,
    p_at timestamptz
) RETURNS jsonb AS $$
DECLARE
    v_first_id bigint;
    v_operation text;
    v_old_data jsonb;
    v_new_data jsonb;
    v_spec jsonb;
    v_undo jsonb;
    v_has_events boolean;
BEGIN
    -- The suffix: every event from the first one (in id order) written
    -- after p_at. changed_at is the transaction start, so a transaction
    -- that started earlier can still write later; id is the write order.
    SELECT min(id) INTO v_first_id
    FROM {audit_table}
    WHERE table_name = p_table AND object_id = p_object_id AND changed_at > p_at;

    IF v_first_id IS NOT NULL THEN
        SELECT operation, old_data INTO v_operation, v_old_data
        FROM {audit_table}
        WHERE table_name = p_table AND object_id = p_object_id AND id = v_first_id;
        IF v_operation = 'INSERT' THEN
            RETURN NULL;
        END IF;
        IF v_old_data IS NOT NULL THEN
            RETURN v_old_data;
        END IF;
        v_spec := _audit_tracked_spec(p_table);
        IF v_spec IS NULL THEN
            RAISE EXCEPTION 'auditrum: table % has diff-only audit rows but no readable entry in {tracking_table} (missing row or no SELECT privilege)', p_table;
        END IF;
        -- Undoing the suffix restores, per column, the "old" of the
        -- earliest suffix event that touched it.
        SELECT jsonb_object_agg(s.key, s.old_value) INTO v_undo
        FROM (
            SELECT DISTINCT ON (d.key) d.key, d.value -> 'old' AS old_value
            FROM {audit_table} a, jsonb_each(a.diff) AS d(key, value)
            WHERE a.table_name = p_table AND a.object_id = p_object_id AND a.id >= v_first_id
            ORDER BY d.key, a.id
        ) s;
        RETURN COALESCE(_audit_live_row(p_table, p_object_id, v_spec), '{{}}'::jsonb)
            || COALESCE(v_undo, '{{}}'::jsonb);
    END IF;

    SELECT operation, new_data INTO v_operation, v_new_data
    FROM {audit_table}
    WHERE table_name = p_table AND object_id = p_object_id
    ORDER BY id DESC
    LIMIT 1;
    v_has_events := FOUND;
    IF v_has_events THEN
        IF v_new_data IS NOT NULL THEN
            RETURN v_new_data;
        END IF;
        IF v_operation = 'DELETE' THEN
            RETURN NULL;
        END IF;
    END IF;

    v_spec := _audit_tracked_spec(p_table);
    IF v_has_events THEN
        -- The latest event is diff-only, so the live row is the current state
        -- whatever mode the table is in now.
        IF v_spec IS NULL THEN
            RAISE EXCEPTION 'auditrum: table % has diff-only audit rows but no readable entry in {tracking_table} (missing row or no SELECT privilege)', p_table;
        END IF;
        RETURN _audit_live_row(p_table, p_object_id, v_spec);
    END IF;
    IF v_spec IS NULL OR v_spec->>'storage' IS DISTINCT FROM 'diff' THEN
        RETURN NULL;
    END IF;
    RETURN _audit_live_row(p_table, p_object_id, v_spec);
END;
$$ LANGUAGE plpgsql STABLE SECURITY INVOKER;
