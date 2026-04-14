CREATE OR REPLACE FUNCTION _audit_reconstruct_row(
    p_table text,
    p_object_id text,
    p_at timestamptz
) RETURNS jsonb AS $$
    SELECT CASE WHEN operation = 'DELETE' THEN NULL ELSE new_data END
    FROM {audit_table}
    WHERE table_name = p_table
      AND object_id = p_object_id
      AND changed_at <= p_at
    ORDER BY changed_at DESC, id DESC
    LIMIT 1;
$$ LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public;
