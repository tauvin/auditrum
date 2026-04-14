
from auditrum.context import AuditContext, audit_context


class TestAuditContextBasic:
    def test_set_and_get(self):
        ctx = AuditContext()
        ctx.set("user_id", 42)
        assert ctx.get("user_id") == 42

    def test_unknown_key_returns_none(self):
        ctx = AuditContext()
        assert ctx.get("missing") is None

    def test_change_reason_stack(self):
        ctx = AuditContext()
        ctx.push_change_reason("outer")
        ctx.push_change_reason("inner")
        assert ctx.get_change_reason() == "outer -> inner"

    def test_build_sql_returns_set_config_calls(self):
        ctx = AuditContext()
        ctx.set("user_id", 42)
        ctx.set("source", "http")
        sql = ctx.build_sql()
        assert "set_config('session.myapp_user_id', '42', true)" in sql
        assert "set_config('session.myapp_source', 'http', true)" in sql

    def test_build_sql_includes_change_reason(self):
        ctx = AuditContext()
        ctx.push_change_reason("test reason")
        sql = ctx.build_sql()
        assert "set_config('session.myapp_change_reason', 'test reason', true)" in sql

    def test_build_sql_escapes_single_quotes(self):
        ctx = AuditContext()
        ctx.set("username", "o'brien")
        sql = ctx.build_sql()
        assert "'o''brien'" in sql

    def test_build_sql_skips_invalid_keys(self):
        ctx = AuditContext()
        ctx.set("bad key", "x")
        ctx.set("user_id", 1)
        sql = ctx.build_sql()
        assert "bad key" not in sql
        assert "user_id" in sql

    def test_none_values_become_empty(self):
        ctx = AuditContext()
        ctx.set("user_id", None)
        sql = ctx.build_sql()
        assert "set_config('session.myapp_user_id', '', true)" in sql


class TestGlobalAuditContext:
    def test_global_instance_exists(self):
        assert audit_context is not None
        assert isinstance(audit_context, AuditContext)

    def test_global_isolation_between_tests(self):
        # conftest autouse fixture clears state — this test verifies clean slate
        assert audit_context.get("user_id") is None
        assert audit_context.get_change_reason() == ""
