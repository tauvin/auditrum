from datetime import UTC, datetime, timedelta

from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_context_table_sql,
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
)
from auditrum.triggers import generate_trigger_sql


class TestPartitioning:
    def test_default_partition_accepts_out_of_range_writes(self, pg_conn):
        """Regression: ensure a missing cron job does not break writes.

        With DEFAULT partition in place, writes outside the month-partitioned
        ranges still land in auditlog_default instead of raising
        'no partition of relation "auditlog" found for row'.
        """
        with pg_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
            cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
            cur.execute("DROP TABLE IF EXISTS widgets CASCADE")
            cur.execute("DROP FUNCTION IF EXISTS jsonb_diff(jsonb, jsonb) CASCADE")
            cur.execute("DROP FUNCTION IF EXISTS _audit_attach_context() CASCADE")
            cur.execute(generate_audit_context_table_sql("audit_context"))
            cur.execute(generate_auditlog_table_sql("auditlog"))
            cur.execute(generate_jsonb_diff_function_sql())
            cur.execute(generate_audit_attach_context_sql("audit_context"))
            # Only one month of explicit partitions
            cur.execute(generate_auditlog_partitions_sql("auditlog", months_ahead=1))
            cur.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
            cur.execute(generate_trigger_sql("widgets"))

            # Force changed_at 2 years into the future (would miss any rolling partition)
            future = (datetime.now(UTC) + timedelta(days=730)).isoformat()
            cur.execute(f"ALTER TABLE auditlog ALTER COLUMN changed_at SET DEFAULT '{future}'")
            cur.execute("INSERT INTO widgets (name) VALUES ('future')")

            cur.execute("SELECT COUNT(*) FROM auditlog_default")
            assert cur.fetchone()[0] == 1

            cur.execute("ALTER TABLE auditlog ALTER COLUMN changed_at SET DEFAULT now()")
            cur.execute("DROP TABLE auditlog CASCADE")
            cur.execute("DROP TABLE widgets CASCADE")
