from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from auditrum.context import audit_context
from auditrum.utils import audit_tracked


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    @contextmanager
    def cursor(self):
        cur = MagicMock()
        cur.execute.side_effect = lambda *args, **kwargs: self.calls.append((args, kwargs))
        yield cur


@pytest.fixture
def recording_executor():
    rec = RecordingExecutor()
    prev = audit_context.get_executor()
    audit_context.set_executor(rec)
    yield rec
    audit_context.set_executor(prev)


class TestAuditTracked:
    def test_reexported_from_auditrum(self):
        import auditrum

        assert auditrum.audit_tracked is audit_tracked

    def test_sets_ctx_and_restores(self, recording_executor):
        with audit_tracked(source="cron", user_id=1):
            assert audit_context.get("source") == "cron"
            assert audit_context.get("user_id") == 1
        assert audit_context.get("source") is None
        assert audit_context.get("user_id") is None

    def test_change_reason_pushed(self, recording_executor):
        with audit_tracked(change_reason="nightly sync", source="cron"):
            assert "nightly sync" in audit_context.get_change_reason()

    def test_applies_set_config_on_cursor(self, recording_executor):
        with audit_tracked(source="cron", user_id=42):
            pass
        queries = [args[0] for args, _ in recording_executor.calls]
        params = [args[1] for args, _ in recording_executor.calls if len(args) > 1]
        assert any("set_config" in q for q in queries)
        assert any(p and p[0] == "session.myapp_source" and p[1] == "cron" for p in params)
