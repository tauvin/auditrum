import pytest

from auditrum.revert import generate_revert_sql


class TestGenerateRevertSql:
    def test_valid_query(self):
        sql = generate_revert_sql("auditlog", "users", "42", 7, ["name", "email"])
        assert '"auditlog"' in sql
        assert '"users"' in sql
        assert '"name"' in sql
        assert '"email"' in sql
        assert "'42'" in sql
        assert "WHERE id = 7" in sql

    def test_record_id_is_quoted_and_escaped(self):
        # record_id with a quote must be safely escaped
        sql = generate_revert_sql("auditlog", "users", "abc' OR '1'='1", 1, ["name"])
        assert "'abc'' OR ''1''=''1'" in sql

    def test_rejects_injection_in_table_name(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            generate_revert_sql("auditlog", "users; DROP TABLE users", "1", 1, ["name"])

    def test_rejects_injection_in_audit_table(self):
        with pytest.raises(ValueError, match="Invalid audit_table"):
            generate_revert_sql("audit log", "users", "1", 1, ["name"])

    def test_rejects_injection_in_columns(self):
        with pytest.raises(ValueError, match="Invalid revert column"):
            generate_revert_sql("auditlog", "users", "1", 1, ["name; DROP"])

    def test_log_id_must_be_int(self):
        with pytest.raises((ValueError, TypeError)):
            generate_revert_sql("auditlog", "users", "1", "not-an-int", ["name"])  # type: ignore[arg-type]

    def test_no_raw_fstring_interpolation(self):
        """Ensure the record_id from user input cannot break out of the literal."""
        payload = "1'; DELETE FROM users; --"
        sql = generate_revert_sql("auditlog", "users", payload, 1, ["name"])
        # The malicious payload must appear as an escaped literal, not loose SQL
        assert "DELETE FROM users" not in sql.upper().replace("'", "").replace(" ", "")
        assert "'1''; DELETE FROM users; --'" in sql
