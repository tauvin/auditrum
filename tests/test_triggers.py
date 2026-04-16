import pytest

from auditrum.triggers import generate_trigger_sql, validate_identifier


class TestValidateIdent:
    def test_accepts_snake_case(self):
        assert validate_identifier("users", "table_name") == "users"
        assert validate_identifier("my_table_v2", "table_name") == "my_table_v2"
        assert validate_identifier("_private", "table_name") == "_private"

    @pytest.mark.parametrize(
        "bad",
        [
            "users; DROP TABLE users",
            "users'--",
            "1users",
            "table-name",
            "table name",
            "",
            "users;--",
            '"; DELETE FROM auditlog; --',
        ],
    )
    def test_rejects_injection(self, bad):
        with pytest.raises(ValueError, match="Invalid table_name"):
            validate_identifier(bad, "table_name")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            validate_identifier(None, "table_name")  # type: ignore[arg-type]


class TestGenerateTriggerSql:
    def test_valid_minimal(self):
        sql = generate_trigger_sql("users")
        assert "CREATE OR REPLACE FUNCTION audit_users_trigger()" in sql
        assert "CREATE TRIGGER audit_users_trigger" in sql
        assert "ON users" in sql
        assert "INSERT INTO auditlog" in sql

    def test_track_only(self):
        sql = generate_trigger_sql("users", track_only=["name", "email"])
        assert "'name'" in sql
        assert "'email'" in sql
        assert "NOT IN" in sql

    def test_exclude_fields(self):
        sql = generate_trigger_sql("users", exclude_fields=["password", "token"])
        assert "'password'" in sql
        assert "'token'" in sql

    def test_track_only_and_exclude_together_raises(self):
        with pytest.raises(ValueError, match="Cannot specify both"):
            generate_trigger_sql("users", track_only=["name"], exclude_fields=["password"])

    def test_custom_audit_table(self):
        sql = generate_trigger_sql("users", audit_table="custom_log")
        assert "INSERT INTO custom_log" in sql

    @pytest.mark.parametrize("bad", ["users; DROP TABLE users", "1bad", "bad-name"])
    def test_rejects_injection_in_table_name(self, bad):
        with pytest.raises(ValueError, match="Invalid table_name"):
            generate_trigger_sql(bad)

    def test_rejects_injection_in_audit_table(self):
        with pytest.raises(ValueError, match="Invalid audit_table"):
            generate_trigger_sql("users", audit_table="log; DROP TABLE log")

    def test_rejects_injection_in_track_only(self):
        with pytest.raises(ValueError, match="Invalid track_only field"):
            generate_trigger_sql("users", track_only=["name'; DELETE"])

    def test_rejects_injection_in_exclude_fields(self):
        with pytest.raises(ValueError, match="Invalid exclude_fields field"):
            generate_trigger_sql("users", exclude_fields=["password; DROP"])

    def test_rejects_injection_in_extra_meta_fields(self):
        with pytest.raises(ValueError, match="Invalid extra_meta_fields field"):
            generate_trigger_sql("users", extra_meta_fields=["bad; SELECT"])

    def test_extra_meta_fields_embedded(self):
        sql = generate_trigger_sql("users", extra_meta_fields=["tenant_id"])
        assert "'tenant_id'" in sql
        assert "to_jsonb(NEW.tenant_id)" in sql

    def test_returns_stripped_sql(self):
        sql = generate_trigger_sql("users")
        assert sql == sql.strip()

    def test_calls_audit_attach_context(self):
        sql = generate_trigger_sql("users")
        assert "_audit_attach_context()" in sql
        assert "context_id" in sql

    def test_calls_audit_current_user_id(self):
        sql = generate_trigger_sql("users")
        assert "_audit_current_user_id()" in sql

    def test_function_is_security_definer(self):
        """Triggers must run as their owner (admin), not the calling app role,
        so the app role can have INSERT on auditlog revoked entirely."""
        sql = generate_trigger_sql("users")
        assert "SECURITY DEFINER" in sql
        assert "SET search_path = pg_catalog, public" in sql

    def test_log_conditions_embedded(self):
        sql = generate_trigger_sql("subs", log_conditions="NEW.is_active = TRUE")
        assert "IF NOT (NEW.is_active = TRUE) THEN" in sql

    def test_no_print_on_stdout(self, capsys):
        generate_trigger_sql("users")
        captured = capsys.readouterr()
        assert captured.out == ""
