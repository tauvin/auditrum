import pytest

from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_context_table_sql,
    generate_audit_current_user_id_sql,
    generate_audit_reconstruct_sql,
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
)


class TestGenerateAuditlogTableSql:
    def test_default_table_name(self):
        sql = generate_auditlog_table_sql()
        assert "CREATE TABLE IF NOT EXISTS auditlog" in sql
        assert "PARTITION BY RANGE (changed_at)" in sql

    def test_default_partition_present(self):
        sql = generate_auditlog_table_sql()
        assert "PARTITION OF auditlog DEFAULT" in sql
        assert "auditlog_default" in sql

    def test_no_expired_initial_partition(self):
        """Regression: earlier schema shipped with a partition ending 2025-01-01
        which would block writes into tracked tables as soon as 'now' > 2025."""
        sql = generate_auditlog_table_sql()
        assert "'2025-01-01'" not in sql
        assert "2000-01-01" not in sql

    def test_gin_index_on_diff(self):
        sql = generate_auditlog_table_sql()
        assert "GIN (diff)" in sql

    def test_context_id_column_and_index(self):
        sql = generate_auditlog_table_sql()
        assert "context_id uuid" in sql
        assert "context_id_idx" in sql

    def test_composite_target_index(self):
        sql = generate_auditlog_table_sql()
        assert "auditlog_target_idx" in sql
        assert "(table_name, object_id, changed_at DESC)" in sql

    def test_object_id_not_standalone_indexed(self):
        """Standalone object_id index is subsumed by the composite target index."""
        sql = generate_auditlog_table_sql()
        assert "auditlog_object_id_idx" not in sql

    def test_no_content_type_id_column(self):
        """The legacy ``content_type_id`` column is gone as of 0.4.

        The column was Django-specific dead weight — never populated by
        the framework-agnostic trigger path — and its absence keeps
        :class:`AuditLog` queries routed through ``table_name`` (the
        canonical identity key), not a NULL GenericForeignKey.
        """
        sql = generate_auditlog_table_sql()
        assert "content_type_id" not in sql

    def test_custom_table_name(self):
        sql = generate_auditlog_table_sql("my_audit")
        assert "CREATE TABLE IF NOT EXISTS my_audit" in sql
        assert "my_audit_default" in sql

    def test_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            generate_auditlog_table_sql("log; DROP TABLE log")


class TestGenerateAuditlogPartitionsSql:
    def test_generates_n_partitions(self):
        sql = generate_auditlog_partitions_sql(months_ahead=3)
        assert sql.count("CREATE TABLE IF NOT EXISTS") == 3

    def test_partitions_are_month_aligned(self):
        sql = generate_auditlog_partitions_sql(months_ahead=2)
        # Each partition covers exactly one month
        assert sql.count("PARTITION OF auditlog") == 2

    def test_custom_table(self):
        sql = generate_auditlog_partitions_sql("events", months_ahead=1)
        assert "events_p" in sql
        assert "PARTITION OF events" in sql

    def test_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            generate_auditlog_partitions_sql("log; DROP", months_ahead=1)


class TestGenerateAuditContextTableSql:
    def test_default_table(self):
        sql = generate_audit_context_table_sql()
        assert "CREATE TABLE IF NOT EXISTS audit_context" in sql
        assert "id uuid PRIMARY KEY" in sql
        assert "metadata jsonb NOT NULL" in sql

    def test_gin_index_on_metadata(self):
        sql = generate_audit_context_table_sql()
        assert "USING GIN (metadata)" in sql

    def test_custom_table(self):
        sql = generate_audit_context_table_sql("my_ctx")
        assert "CREATE TABLE IF NOT EXISTS my_ctx" in sql

    def test_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid context_table"):
            generate_audit_context_table_sql("audit_context; DROP")


class TestGenerateAuditAttachContextSql:
    def test_default(self):
        sql = generate_audit_attach_context_sql()
        assert "CREATE OR REPLACE FUNCTION _audit_attach_context()" in sql
        assert "RETURNS uuid" in sql
        assert "current_setting('auditrum.context_id')" in sql
        assert "current_setting('auditrum.context_metadata')" in sql
        assert "ON CONFLICT (id) DO UPDATE" in sql

    def test_handles_missing_guc_gracefully(self):
        sql = generate_audit_attach_context_sql()
        assert "EXCEPTION WHEN OTHERS THEN" in sql
        assert "RETURN NULL" in sql

    def test_is_security_definer(self):
        sql = generate_audit_attach_context_sql()
        assert "SECURITY DEFINER" in sql
        assert "SET search_path = pg_catalog, public" in sql

    def test_custom_gucs(self):
        sql = generate_audit_attach_context_sql(
            guc_id="myapp.ctx_id", guc_metadata="myapp.ctx_meta"
        )
        assert "current_setting('myapp.ctx_id')" in sql
        assert "current_setting('myapp.ctx_meta')" in sql

    def test_rejects_injection_in_context_table(self):
        with pytest.raises(ValueError, match="Invalid context_table"):
            generate_audit_attach_context_sql("bad; DROP")


class TestGenerateAuditCurrentUserIdSql:
    def test_returns_integer_function(self):
        sql = generate_audit_current_user_id_sql()
        assert "CREATE OR REPLACE FUNCTION _audit_current_user_id()" in sql
        assert "RETURNS integer" in sql

    def test_reads_metadata_guc_and_casts(self):
        sql = generate_audit_current_user_id_sql()
        assert "current_setting('auditrum.context_metadata')" in sql
        assert "_meta->>'user_id')::integer" in sql

    def test_handles_missing_gracefully(self):
        sql = generate_audit_current_user_id_sql()
        assert "EXCEPTION WHEN OTHERS THEN" in sql
        assert "RETURN NULL" in sql

    def test_custom_guc_name(self):
        sql = generate_audit_current_user_id_sql(guc_metadata="myapp.ctx_meta")
        assert "current_setting('myapp.ctx_meta')" in sql

    def test_is_security_definer(self):
        sql = generate_audit_current_user_id_sql()
        assert "SECURITY DEFINER" in sql
        assert "SET search_path = pg_catalog, public" in sql


class TestGenerateAuditReconstructSql:
    def test_emits_both_functions(self):
        sql = generate_audit_reconstruct_sql()
        assert "CREATE OR REPLACE FUNCTION _audit_reconstruct_row" in sql
        assert "CREATE OR REPLACE FUNCTION _audit_reconstruct_table" in sql

    def test_row_function_returns_jsonb(self):
        sql = generate_audit_reconstruct_sql()
        assert "RETURNS jsonb" in sql

    def test_table_function_returns_setof(self):
        sql = generate_audit_reconstruct_sql()
        assert "RETURNS TABLE" in sql
        assert "object_id text" in sql
        assert "row_data jsonb" in sql

    def test_filters_delete_to_null(self):
        sql = generate_audit_reconstruct_sql()
        assert "CASE WHEN operation = 'DELETE' THEN NULL ELSE new_data END" in sql

    def test_orders_by_changed_at_desc(self):
        sql = generate_audit_reconstruct_sql()
        assert "ORDER BY changed_at DESC, id DESC" in sql
        assert "LIMIT 1" in sql

    def test_custom_audit_table(self):
        sql = generate_audit_reconstruct_sql("custom_log")
        assert "FROM custom_log" in sql

    def test_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid audit_table"):
            generate_audit_reconstruct_sql("bad; DROP")

    def test_functions_are_stable(self):
        sql = generate_audit_reconstruct_sql()
        # STABLE may be on its own line after LANGUAGE sql; match both
        assert "LANGUAGE sql" in sql
        assert "STABLE" in sql

    def test_is_security_definer(self):
        sql = generate_audit_reconstruct_sql()
        assert sql.count("SECURITY DEFINER") == 2
        assert sql.count("SET search_path = pg_catalog, public") == 2


class TestJsonbDiffFunction:
    def test_returns_plpgsql(self):
        sql = generate_jsonb_diff_function_sql()
        assert "CREATE OR REPLACE FUNCTION jsonb_diff" in sql
        assert "LANGUAGE plpgsql" in sql

    def test_emits_paired_format(self):
        """Regression: the diff output is paired ``{old, new}``, not
        values-only. The 0.3.x shape forced every UI consumer to
        cross-reference ``old_data`` and dropped null-value updates
        through ``jsonb_strip_nulls``. Paired form is self-sufficient.
        """
        sql = generate_jsonb_diff_function_sql()
        assert "jsonb_build_object('old', old -> key, 'new', value)" in sql
        # Values-only shape must not reappear — guard against future
        # accidental regressions to the pre-0.4 format.
        assert "jsonb_object_agg(key, value)" not in sql
