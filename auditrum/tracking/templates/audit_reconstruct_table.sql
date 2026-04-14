CREATE OR REPLACE FUNCTION _audit_reconstruct_table(
    p_table text,
    p_at timestamptz
) RETURNS TABLE(object_id text, row_data jsonb) AS $$
    SELECT latest.object_id, latest.new_data
    FROM (
        SELECT DISTINCT ON (a.object_id)
            a.object_id, a.operation, a.new_data
        FROM {audit_table} a
        WHERE a.table_name = p_table
          AND a.changed_at <= p_at
        ORDER BY a.object_id, a.changed_at DESC, a.id DESC
    ) latest
    WHERE latest.operation <> 'DELETE';
$$ LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public;
