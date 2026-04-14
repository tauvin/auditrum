import pytest

from auditrum.hardening import generate_grant_admin_sql, generate_revoke_sql


class TestRevoke:
    def test_revokes_all_writes_from_public_by_default(self):
        sql = generate_revoke_sql("auditlog")
        assert "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON auditlog FROM PUBLIC" in sql

    def test_revokes_from_context_table_too(self):
        """Both auditlog and audit_context must be locked down — triggers
        upsert into audit_context via _audit_attach_context()."""
        sql = generate_revoke_sql("auditlog")
        assert "ON audit_context FROM PUBLIC" in sql

    def test_custom_context_table(self):
        sql = generate_revoke_sql("auditlog", context_table="ctx")
        assert "ON ctx FROM PUBLIC" in sql

    def test_insert_is_revoked_too(self):
        """Regression from 0.2: INSERT used to be 'left intact' which broke
        the append-only story because the app role could forge rows."""
        sql = generate_revoke_sql("auditlog")
        assert "INSERT" in sql

    def test_select_is_not_revoked(self):
        """App role still needs to read the audit log (for history views)."""
        sql = generate_revoke_sql("auditlog")
        assert "REVOKE SELECT" not in sql

    def test_also_revokes_from_app_role(self):
        sql = generate_revoke_sql("auditlog", app_role="myapp")
        assert "FROM PUBLIC" in sql
        assert "FROM myapp" in sql

    def test_rejects_injection_in_table(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            generate_revoke_sql("auditlog; DROP", app_role="r")

    def test_rejects_injection_in_context_table(self):
        with pytest.raises(ValueError, match="Invalid context_table"):
            generate_revoke_sql("auditlog", context_table="ctx; DROP")

    def test_rejects_injection_in_role(self):
        with pytest.raises(ValueError, match="Invalid app_role"):
            generate_revoke_sql("auditlog", app_role="r; DROP USER")


class TestGrantAdmin:
    def test_grants_full_privs_on_both_tables(self):
        sql = generate_grant_admin_sql("auditlog", "audit_admin")
        assert "GRANT" in sql
        assert "TO audit_admin" in sql
        assert "ON auditlog" in sql
        assert "ON audit_context" in sql

    def test_insert_is_granted(self):
        """Admin role owns the SECURITY DEFINER trigger functions and must
        have INSERT so those functions can write audit rows."""
        sql = generate_grant_admin_sql("auditlog", "audit_admin")
        assert "INSERT" in sql

    def test_custom_context_table(self):
        sql = generate_grant_admin_sql("auditlog", "admin", context_table="ctx")
        assert "ON ctx TO admin" in sql

    def test_rejects_injection_in_admin_role(self):
        with pytest.raises(ValueError, match="Invalid admin_role"):
            generate_grant_admin_sql("auditlog", "admin; DROP")

    def test_rejects_injection_in_context_table(self):
        with pytest.raises(ValueError, match="Invalid context_table"):
            generate_grant_admin_sql("auditlog", "admin", context_table="ctx; DROP")
