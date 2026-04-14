from contextlib import contextmanager
from unittest.mock import MagicMock

from auditrum.context import AuditContext, audit_context
from auditrum.executor import ConnectionExecutor, NullExecutor, PsycopgExecutor


class TestNullExecutor:
    def test_cursor_context_manager(self):
        ex = NullExecutor()
        with ex.cursor() as cur:
            assert cur is not None
            # execute is a no-op but must exist
            cur.execute("SELECT 1")

    def test_satisfies_protocol(self):
        assert isinstance(NullExecutor(), ConnectionExecutor)


class TestPsycopgExecutor:
    def test_delegates_to_underlying_conn(self):
        fake_conn = MagicMock()
        fake_cursor = MagicMock()
        fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
        fake_cursor.__exit__ = MagicMock(return_value=False)
        fake_conn.cursor.return_value = fake_cursor

        ex = PsycopgExecutor(fake_conn)
        with ex.cursor() as cur:
            cur.execute("SELECT 1")

        fake_conn.cursor.assert_called_once()
        fake_cursor.execute.assert_called_once_with("SELECT 1")


class TestAuditContextExecutor:
    def test_default_is_null_executor(self):
        ctx = AuditContext()
        assert isinstance(ctx.get_executor(), NullExecutor)

    def test_set_executor(self):
        ctx = AuditContext()
        fake = NullExecutor()
        ctx.set_executor(fake)
        assert ctx.get_executor() is fake

    def test_use_without_executor_does_not_raise(self):
        ctx = AuditContext()
        with ctx.use(user_id=1, source="test"):
            assert ctx.get("user_id") == 1
        assert ctx.get("user_id") is None

    def test_use_invokes_executor_cursor(self):
        calls = []

        class RecExec:
            @contextmanager
            def cursor(self):
                cur = MagicMock()
                cur.execute = lambda *a, **k: calls.append((a, k))
                yield cur

        ctx = AuditContext(executor=RecExec())
        with ctx.use(user_id=42, source="cli"):
            pass
        queries = [c[0][0] for c in calls]
        assert any("set_config" in q for q in queries)

    def test_global_context_has_null_executor_by_default(self):
        # After import, global instance should default to NullExecutor
        assert audit_context.get_executor() is not None
