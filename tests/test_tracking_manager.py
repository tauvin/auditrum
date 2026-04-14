"""Unit tests for TriggerManager with an in-memory fake executor.

No real database — we stub a tiny SQL runner that captures executed
statements and mimics the tracking-table round-trip. This covers the
state transitions (NOT_INSTALLED → INSTALLED → DRIFT → INSTALLED via
sync) without a PG dependency.
"""

from contextlib import contextmanager

import pytest

from auditrum.tracking import (
    FieldFilter,
    SyncReport,
    TrackSpec,
    TriggerAction,
    TriggerManager,
    TriggerStatus,
)


class FakeCursor:
    """Minimal DB-API cursor emulating what TriggerManager needs.

    Maintains an in-memory ``tracking`` dict keyed by trigger_name and
    records all executed SQL in ``executed``. Handles the specific queries
    TriggerManager issues: CREATE TABLE IF NOT EXISTS, SELECT by
    trigger_name, INSERT ON CONFLICT, DELETE, and SELECT ORDER BY
    trigger_name for list_installed. Everything else is a no-op.
    """

    def __init__(self, state):
        self._state = state
        self._result: list = []

    def execute(self, sql, params=None):
        self._state["executed"].append((sql, params))
        sql_lower = sql.strip().lower()
        if sql_lower.startswith("select pg_advisory_xact_lock"):
            self._result = [(1,)]
            return
        if sql_lower.startswith("create table"):
            self._result = []
            return
        if sql_lower.startswith("select checksum"):
            row = self._state["tracking"].get(params[0])
            self._result = [row] if row else []
            return
        if sql_lower.startswith("select trigger_name"):
            rows = [
                (name, fp["table"], chk, "2026-04-14T00:00:00Z", fp)
                for name, (chk, fp) in self._state["tracking"].items()
            ]
            self._result = sorted(rows)
            return
        if sql_lower.startswith("insert into"):
            name, table, checksum, fingerprint = params
            import json

            self._state["tracking"][name] = (checksum, json.loads(fingerprint))
            self._result = []
            return
        if sql_lower.startswith("delete from"):
            self._state["tracking"].pop(params[0], None)
            self._result = []
            return
        # CREATE/DROP FUNCTION, CREATE/DROP TRIGGER, etc.
        self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeExecutor:
    def __init__(self):
        self.state = {"executed": [], "tracking": {}}

    @contextmanager
    def cursor(self):
        yield FakeCursor(self.state)


@pytest.fixture
def mgr():
    return TriggerManager(FakeExecutor())


@pytest.fixture
def spec():
    return TrackSpec(table="users", fields=FieldFilter.only("name", "email"))


class TestBootstrap:
    def test_creates_tracking_table(self, mgr):
        mgr.bootstrap()
        statements = [sql for sql, _ in mgr.executor.state["executed"]]
        assert any(
            "CREATE TABLE IF NOT EXISTS auditrum_applied_triggers" in s
            for s in statements
        )

    def test_idempotent(self, mgr):
        mgr.bootstrap()
        mgr.bootstrap()  # no crash, still fine
        statements = [sql for sql, _ in mgr.executor.state["executed"]]
        assert sum(1 for s in statements if "CREATE TABLE IF NOT EXISTS" in s) == 2


class TestInspect:
    def test_not_installed_initially(self, mgr, spec):
        assert mgr.inspect(spec) == TriggerStatus.NOT_INSTALLED

    def test_installed_after_install(self, mgr, spec):
        mgr.install(spec)
        assert mgr.inspect(spec) == TriggerStatus.INSTALLED

    def test_drift_when_spec_changes(self, mgr, spec):
        mgr.install(spec)
        changed = TrackSpec(table="users", fields=FieldFilter.only("name"))
        assert mgr.inspect(changed) == TriggerStatus.DRIFT


class TestInstall:
    def test_first_install_runs_ddl(self, mgr, spec):
        changed = mgr.install(spec)
        assert changed is True
        statements = [sql for sql, _ in mgr.executor.state["executed"]]
        assert any("CREATE OR REPLACE FUNCTION audit_users_trigger" in s for s in statements)
        assert any("CREATE TRIGGER audit_users_trigger" in s for s in statements)

    def test_second_install_is_noop_when_same(self, mgr, spec):
        mgr.install(spec)
        before = len(mgr.executor.state["executed"])
        changed = mgr.install(spec)
        after = len(mgr.executor.state["executed"])
        assert changed is False
        # Second call only does advisory lock + stored lookup, no DDL
        new_statements = mgr.executor.state["executed"][before:after]
        assert not any("CREATE OR REPLACE FUNCTION" in s for s, _ in new_statements)

    def test_force_reinstalls(self, mgr, spec):
        mgr.install(spec)
        before = len(mgr.executor.state["executed"])
        changed = mgr.install(spec, force=True)
        after = len(mgr.executor.state["executed"])
        assert changed is True
        new_statements = [s for s, _ in mgr.executor.state["executed"][before:after]]
        assert any("CREATE OR REPLACE FUNCTION" in s for s in new_statements)

    def test_advisory_lock_acquired_and_released(self, mgr, spec):
        """Session-level advisory lock taken before DDL and released after.

        Was transaction-scoped (``pg_advisory_xact_lock``) until 0.3.1,
        but that path silently does nothing with autocommit cursors
        because each statement is its own implicit transaction.
        """
        mgr.install(spec)
        statements = [sql for sql, _ in mgr.executor.state["executed"]]
        assert any("pg_advisory_lock(hashtextextended" in s for s in statements)
        assert any("pg_advisory_unlock(hashtextextended" in s for s in statements)


class TestUninstall:
    def test_removes_tracking_row(self, mgr, spec):
        mgr.install(spec)
        assert mgr.inspect(spec) == TriggerStatus.INSTALLED
        mgr.uninstall(spec)
        assert mgr.inspect(spec) == TriggerStatus.NOT_INSTALLED

    def test_emits_drop_statements(self, mgr, spec):
        mgr.install(spec)
        mgr.uninstall(spec)
        statements = [sql for sql, _ in mgr.executor.state["executed"]]
        assert any("DROP TRIGGER IF EXISTS audit_users_trigger" in s for s in statements)
        assert any("DROP FUNCTION IF EXISTS audit_users_trigger" in s for s in statements)


class TestDiff:
    def test_install_action_when_missing(self, mgr, spec):
        entries = mgr.diff([spec])
        assert len(entries) == 1
        assert entries[0].action == TriggerAction.INSTALL
        assert entries[0].status_before == TriggerStatus.NOT_INSTALLED

    def test_skip_action_when_up_to_date(self, mgr, spec):
        mgr.install(spec)
        entries = mgr.diff([spec])
        assert entries[0].action == TriggerAction.SKIP

    def test_update_action_on_drift(self, mgr, spec):
        mgr.install(spec)
        changed = TrackSpec(table="users", fields=FieldFilter.only("name"))
        entries = mgr.diff([changed])
        assert entries[0].action == TriggerAction.UPDATE

    def test_prune_emits_uninstall_for_orphans(self, mgr, spec):
        mgr.install(spec)
        other = TrackSpec(table="orders")
        entries = mgr.diff([other], prune=True)
        actions = {(e.trigger_name, e.action) for e in entries}
        assert ("audit_orders_trigger", TriggerAction.INSTALL) in actions
        assert ("audit_users_trigger", TriggerAction.UNINSTALL) in actions


class TestSync:
    def test_report_shape(self, mgr, spec):
        report = mgr.sync([spec])
        assert isinstance(report, SyncReport)
        assert report.installed == ["audit_users_trigger"]
        assert report.updated == []
        assert report.skipped == []
        assert report.total_changes == 1

    def test_noop_second_run(self, mgr, spec):
        mgr.sync([spec])
        report = mgr.sync([spec])
        assert report.installed == []
        assert report.updated == []
        assert report.skipped == ["audit_users_trigger"]
        assert report.total_changes == 0

    def test_update_drifted(self, mgr, spec):
        mgr.sync([spec])
        changed = TrackSpec(table="users", fields=FieldFilter.only("name"))
        report = mgr.sync([changed])
        assert report.updated == ["audit_users_trigger"]
        assert report.installed == []

    def test_prune_removes_orphans(self, mgr, spec):
        mgr.sync([spec])
        other = TrackSpec(table="orders")
        report = mgr.sync([other], prune=True)
        assert report.installed == ["audit_orders_trigger"]
        assert report.uninstalled == ["audit_users_trigger"]

    def test_no_prune_by_default(self, mgr, spec):
        mgr.sync([spec])
        other = TrackSpec(table="orders")
        report = mgr.sync([other])  # prune=False
        assert report.uninstalled == []
        assert report.installed == ["audit_orders_trigger"]
