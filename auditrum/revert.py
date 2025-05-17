from typing import List


def get_revert_columns_from_log(conn, audit_table: str, log_id: int) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT jsonb_object_keys(old_data)
            FROM {audit_table}
            WHERE id = %s
        """,
            (log_id,),
        )
        keys = cur.fetchall()
    return [row[0] for row in keys]


def generate_revert_sql(
    audit_table: str, table_name: str, record_id: str, log_id: int, columns: List[str]
) -> str:
    return f"""
WITH audit_entry AS (
    SELECT old_data
    FROM {audit_table}
    WHERE id = {log_id}
    AND table_name = '{table_name}'
)
UPDATE {table_name}
SET {", ".join([f"{col} = audit_entry.old_data->>'{col}'" for col in columns])}
FROM audit_entry
WHERE {table_name}.id = '{record_id}';
""".strip()


def generate_revert_sql_from_log(
    conn, audit_table: str, table_name: str, record_id: str, log_id: int
) -> str:
    columns = get_revert_columns_from_log(conn, audit_table, log_id)
    return generate_revert_sql(audit_table, table_name, record_id, log_id, columns)
