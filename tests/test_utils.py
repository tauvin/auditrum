import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from auditrum.context import audit_context, with_change_reason, with_context
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


class TestWithContextAsync:
    """Regression: ``@with_context`` on an ``async def`` used to be a
    silent no-op. The sync wrapper closed ``audit_context.use(...)``
    before the coroutine was scheduled, so every event emitted by the
    task body ran with an empty context. The wrapper now auto-detects
    coroutines via :func:`asyncio.iscoroutinefunction` and returns an
    ``async`` wrapper that keeps the context open across every
    ``await`` inside the task.
    """

    def test_sync_still_works(self, recording_executor):
        seen: dict[str, object] = {}

        @with_context(source="job", user_id=7)
        def task():
            seen["source"] = audit_context.get("source")
            seen["user_id"] = audit_context.get("user_id")

        task()
        assert seen == {"source": "job", "user_id": 7}
        # Context restored after the call returns.
        assert audit_context.get("source") is None

    def test_async_preserves_context_across_await(self, recording_executor):
        seen: dict[str, object] = {}

        @with_context(source="worker", user_id=3)
        async def task():
            # An await happens before the snapshot — the old sync
            # wrapper would have closed `use(...)` by now and the get()
            # below would return None.
            await asyncio.sleep(0)
            seen["source"] = audit_context.get("source")
            seen["user_id"] = audit_context.get("user_id")

        asyncio.run(task())
        assert seen == {"source": "worker", "user_id": 3}
        assert audit_context.get("source") is None

    def test_async_wrapper_is_coroutine_function(self):
        @with_context(source="x")
        async def task():
            return 1

        assert asyncio.iscoroutinefunction(task)


class TestWithChangeReasonAsync:
    def test_sync_still_works(self, recording_executor):
        seen: dict[str, str] = {}

        @with_change_reason("nightly sync")
        def task():
            seen["reason"] = audit_context.get_change_reason()

        task()
        assert "nightly sync" in seen["reason"]
        assert audit_context.get_change_reason() == ""

    def test_async_preserves_reason_across_await(self, recording_executor):
        seen: dict[str, str] = {}

        @with_change_reason("migration backfill")
        async def task():
            await asyncio.sleep(0)
            seen["reason"] = audit_context.get_change_reason()

        asyncio.run(task())
        assert "migration backfill" in seen["reason"]
        assert audit_context.get_change_reason() == ""
