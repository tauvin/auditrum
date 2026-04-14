from psycopg import sql

from auditrum.tracking.spec import validate_identifier


def get_revert_columns_from_log(conn, audit_table: str, log_id: int) -> list[str]:
    """Return the column names that should be restored when reverting a log entry.

    Skips ``id`` because the primary key is invariant — the revert is
    keyed *by* it, restoring it would be a no-op at best and a type
    mismatch at worst (jsonb ``->>`` always returns ``text``, but most
    primary keys are ``integer`` or ``bigint``).
    """
    validate_identifier(audit_table, "audit_table")
    query = sql.SQL("SELECT jsonb_object_keys(old_data) FROM {} WHERE id = %s").format(
        sql.Identifier(audit_table)
    )
    with conn.cursor() as cur:
        cur.execute(query, (int(log_id),))
        keys = cur.fetchall()
    return [row[0] for row in keys if row[0] != "id"]


def generate_revert_sql(
    audit_table: str,
    table_name: str,
    record_id: str,
    log_id: int,
    columns: list[str],
) -> str:
    """Render the revert UPDATE as a safely composed SQL string.

    All identifiers are validated and interpolated via :mod:`psycopg.sql`;
    ``record_id`` and ``log_id`` are bound as literals. The returned string is
    already properly quoted and safe to ``cursor.execute()`` without extra params.
    """
    validate_identifier(audit_table, "audit_table")
    validate_identifier(table_name, "table_name")
    for col in columns:
        validate_identifier(col, "revert column")

    set_items = sql.SQL(", ").join(
        sql.SQL("{col} = audit_entry.old_data->>{col_lit}").format(
            col=sql.Identifier(col),
            col_lit=sql.Literal(col),
        )
        for col in columns
    )

    query = sql.SQL(
        "WITH audit_entry AS ("
        "SELECT old_data FROM {audit_table} "
        "WHERE id = {log_id} AND table_name = {table_lit}"
        ") "
        "UPDATE {table} SET {set_items} FROM audit_entry "
        "WHERE {table}.id = {record_id};"
    ).format(
        audit_table=sql.Identifier(audit_table),
        log_id=sql.Literal(int(log_id)),
        table_lit=sql.Literal(table_name),
        table=sql.Identifier(table_name),
        set_items=set_items,
        record_id=sql.Literal(str(record_id)),
    )
    return query.as_string(None)


def generate_revert_sql_from_log(
    conn, audit_table: str, table_name: str, record_id: str, log_id: int
) -> str:
    columns = get_revert_columns_from_log(conn, audit_table, log_id)
    return generate_revert_sql(audit_table, table_name, record_id, log_id, columns)
