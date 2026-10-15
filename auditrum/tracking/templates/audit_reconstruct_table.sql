CREATE OR REPLACE FUNCTION _audit_reconstruct_table(
    p_table text,
    p_at timestamptz
) RETURNS TABLE(object_id text, row_data jsonb) AS $$
#variable_conflict use_column
DECLARE
    v_spec jsonb := _audit_tracked_spec(p_table);
    v_diff_mode boolean := COALESCE(v_spec->>'storage' = 'diff', false);
    v_has_diff_rows boolean;
BEGIN
    -- Diff-only rows need the live table even if the table is full now.
    v_has_diff_rows := EXISTS (
        SELECT 1 FROM {audit_table} a
        WHERE a.table_name = p_table AND a.diff IS NOT NULL
          AND a.old_data IS NULL AND a.new_data IS NULL
    );
    IF v_spec IS NULL AND v_has_diff_rows THEN
        RAISE EXCEPTION 'auditrum: table % has diff-only audit rows but no readable entry in {tracking_table} (missing row or no SELECT privilege)', p_table;
    END IF;

    -- Same rules as _audit_reconstruct_row, per object_id.
    RETURN QUERY
    WITH ev AS (
        SELECT a.id, a.object_id, a.operation, a.changed_at, a.old_data, a.new_data, a.diff
        FROM {audit_table} a
        WHERE a.table_name = p_table
    ),
    firsts AS (
        SELECT e.object_id, min(e.id) AS first_id
        FROM ev e
        WHERE e.changed_at > p_at
        GROUP BY e.object_id
    ),
    first_ev AS (
        SELECT e.object_id, e.operation, e.old_data
        FROM ev e
        JOIN firsts f ON f.object_id = e.object_id AND f.first_id = e.id
    ),
    undo AS (
        SELECT s.object_id, jsonb_object_agg(s.key, s.old_value) AS undo
        FROM (
            SELECT DISTINCT ON (e.object_id, d.key)
                e.object_id, d.key, d.value -> 'old' AS old_value
            FROM ev e
            JOIN firsts f ON f.object_id = e.object_id AND e.id >= f.first_id
            JOIN first_ev fe ON fe.object_id = e.object_id
                AND fe.operation <> 'INSERT' AND fe.old_data IS NULL
            CROSS JOIN LATERAL jsonb_each(e.diff) AS d(key, value)
            ORDER BY e.object_id, d.key, e.id
        ) s
        GROUP BY s.object_id
    ),
    lasts AS (
        SELECT DISTINCT ON (e.object_id) e.object_id, e.operation, e.new_data
        FROM ev e
        WHERE NOT EXISTS (SELECT 1 FROM firsts f WHERE f.object_id = e.object_id)
        ORDER BY e.object_id, e.id DESC
    ),
    live AS (
        SELECT l.object_id, l.row_data
        FROM _audit_live_rows(
            p_table,
            CASE WHEN v_diff_mode OR v_has_diff_rows THEN v_spec END
        ) l
    ),
    states AS (
        -- objects changed after the cut
        SELECT fe.object_id,
               CASE
                   WHEN fe.old_data IS NOT NULL THEN fe.old_data
                   ELSE COALESCE(lv.row_data, '{{}}'::jsonb) || COALESCE(u.undo, '{{}}'::jsonb)
               END AS row_data
        FROM first_ev fe
        LEFT JOIN undo u ON u.object_id = fe.object_id
        LEFT JOIN live lv ON lv.object_id = fe.object_id
        WHERE fe.operation <> 'INSERT'
        UNION ALL
        -- objects untouched since the cut, with events
        SELECT la.object_id, COALESCE(la.new_data, lv.row_data)
        FROM lasts la
        LEFT JOIN live lv ON lv.object_id = la.object_id
        WHERE la.operation <> 'DELETE'
        UNION ALL
        -- objects without any events (diff tables only)
        SELECT lv.object_id, lv.row_data
        FROM live lv
        WHERE v_diff_mode
          AND NOT EXISTS (SELECT 1 FROM ev e WHERE e.object_id = lv.object_id)
    )
    SELECT st.object_id, st.row_data FROM states st WHERE st.row_data IS NOT NULL;
END;
$$ LANGUAGE plpgsql STABLE SECURITY INVOKER;
